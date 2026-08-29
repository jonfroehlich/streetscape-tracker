"""
Mapillary downloader tests: tile math, MVT decoding, pano filtering, date
handling, grid assignment, and an end-to-end download with synthetic tiles
served from memory. No network.
"""

import asyncio
import gzip
import inspect
import math
import os
import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import geopy
import mapbox_vector_tile
import numpy as np
import pandas as pd
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.config import MAPILLARY_METADATA_DTYPES
from streetscape_metadata_tracker.download_common import (
    HOST_MAPILLARY_TILES,
    DownloadError,
    HostBlockedError,
    HostUnavailableError,
    generate_grid_points,
)
from streetscape_metadata_tracker.json_summarizer import compute_mapillary_meta

SEATTLE = (47.6062, -122.3321)


# ── Tile math ──────────────────────────────────────────────────────────────


def test_tile_frac_known_anchors():
    n = 2**14
    # Greenwich/equator sits exactly at the center of the tile grid
    fx, fy = dm.lonlat_to_tile_frac(0.0, 0.0, 14)
    assert fx == pytest.approx(n / 2)
    assert fy == pytest.approx(n / 2)
    # Antimeridian west edge is tile x=0
    fx, _ = dm.lonlat_to_tile_frac(-180.0, 0.0, 14)
    assert fx == pytest.approx(0.0)


@pytest.mark.parametrize(
    "lon,lat",
    [
        (0.0, 0.0),
        (-122.3321, 47.6062),  # Seattle
        (151.2093, -33.8688),  # Sydney (southern hemisphere)
        (18.9553, 69.6496),  # Tromsø (high latitude)
    ],
)
def test_tile_frac_roundtrip(lon, lat):
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    lon2, lat2 = dm.tile_frac_to_lonlat(fx, fy, 14)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert lat2 == pytest.approx(lat, abs=1e-9)


def test_tiles_for_bbox_single_tile():
    # A bbox strictly inside one tile yields exactly that tile
    fx, fy = dm.lonlat_to_tile_frac(*reversed(SEATTLE), 14)
    lon_mid, lat_mid = dm.tile_frac_to_lonlat(int(fx) + 0.5, int(fy) + 0.5, 14)
    eps = 1e-5
    tiles = dm.tiles_for_bbox(lon_mid - eps, lat_mid - eps, lon_mid + eps, lat_mid + eps)
    assert tiles == [(int(fx), int(fy))]


def test_tiles_for_bbox_straddles_boundary():
    # A bbox centered on a tile corner touches all four neighbors
    fx, fy = dm.lonlat_to_tile_frac(*reversed(SEATTLE), 14)
    x0, y0 = int(fx), int(fy)
    corner_lon, corner_lat = dm.tile_frac_to_lonlat(x0, y0, 14)
    eps = 1e-5
    tiles = set(
        dm.tiles_for_bbox(corner_lon - eps, corner_lat - eps, corner_lon + eps, corner_lat + eps)
    )
    assert tiles == {(x0 - 1, y0 - 1), (x0, y0 - 1), (x0 - 1, y0), (x0, y0)}


def test_tiles_for_bbox_wraps_across_antimeridian():
    # A grid straddling 180° (e.g. Suva, Fiji region) arrives with
    # min_lon > max_lon after geopy normalizes longitudes to ±180. The
    # tile list must wrap and cover columns on BOTH sides of the seam —
    # the naive single x-range was empty, silently yielding a 0-tile run.
    tiles = dm.tiles_for_bbox(179.98, -18.2, -179.98, -18.1, zoom=14)
    assert tiles
    xs = {x for x, _ in tiles}
    n = 2**14
    assert (n - 1) in xs  # easternmost column (just west of 180°)
    assert 0 in xs  # westernmost column (just east of -180°)
    # No spurious mid-ocean columns: only the two seam-adjacent ones
    assert xs == {0, n - 1}


def test_grid_bbox_contains_every_grid_point():
    lat, lon = SEATTLE
    width, height, step = 1000, 600, 20
    min_lon, min_lat, max_lon, max_lat = dm.grid_bbox(lat, lon, width, height, step)
    points = generate_grid_points(
        geopy.Point(lat, lon), int(width / step), int(height / step), step
    )
    for p_lat, p_lon, _, _ in points:
        assert min_lat < p_lat < max_lat
        assert min_lon < p_lon < max_lon


def test_estimate_tile_count_matches_enumeration():
    lat, lon = SEATTLE
    n = dm.estimate_tile_count(lat, lon, 5000, 5000, 20)
    assert n == len(dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 5000, 5000, 20)))
    # A z14 tile at Seattle is ~1.7km wide: a 5km grid needs a 3x4-ish block,
    # never one tile, never hundreds
    assert 9 <= n <= 25


# ── Synthetic tile encoding (shared by decode + end-to-end tests) ──────────


def encode_tile(features, tile_x, tile_y, zoom=14, extent=4096):
    """
    Build raw MVT bytes for the 'image' layer from records with lon/lat and
    Mapillary-style properties, inverting the decode path's coordinate math.
    """
    encoded_features = []
    for f in features:
        fx, fy = dm.lonlat_to_tile_frac(f["lon"], f["lat"], zoom)
        px = (fx - tile_x) * extent
        py = (1 - (fy - tile_y)) * extent  # y-up, matching decode()'s default
        props = {k: v for k, v in f.items() if k not in ("lon", "lat")}
        encoded_features.append(
            {
                "geometry": {"type": "Point", "coordinates": [px, py]},
                "properties": props,
            }
        )
    return mapbox_vector_tile.encode([{"name": dm.IMAGE_LAYER, "features": encoded_features}])


def make_image(
    image_id,
    lon,
    lat,
    *,
    is_pano=True,
    captured_at=1650000000000,
    creator_id=42,
    organization_id=None,
    quality_score=None,
    on_foot=None,
    compass_angle=None,
    sequence_id=None,
):
    img = {
        "id": image_id,
        "lon": lon,
        "lat": lat,
        "is_pano": is_pano,
        "captured_at": captured_at,
        "creator_id": creator_id,
    }
    # Optional free tile extras. Real tiles OMIT a property when absent (e.g.
    # organization_id on individual-contributor imagery), so only include a
    # key when provided — and use the tile's own name `foot` for on_foot.
    optional = {
        "organization_id": organization_id,
        "quality_score": quality_score,
        "foot": on_foot,
        "compass_angle": compass_angle,
        "sequence_id": sequence_id,
    }
    img.update({k: v for k, v in optional.items() if v is not None})
    return img


# ── Decoding ───────────────────────────────────────────────────────────────


def test_decode_keeps_pano_and_flat_tagged():
    # Issue #116: flats are no longer dropped at decode — both kinds are
    # returned, tagged by is_pano, so the caller can stratify coverage.
    lat, lon = SEATTLE
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    x, y = int(fx), int(fy)
    tile = encode_tile(
        [
            make_image(1, lon, lat, is_pano=True),
            make_image(2, lon + 1e-4, lat, is_pano=False),  # flat phone photo
        ],
        x,
        y,
    )
    records = dm.decode_image_features(tile, x, y)
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"1", "2"}
    assert by_id["1"]["is_pano"] is True
    assert by_id["2"]["is_pano"] is False


def test_decode_coordinates_are_accurate():
    # z14 tile resolution is ~2.4m at the equator (extent 4096); decoded
    # positions must land within a few meters of where they were encoded
    lat, lon = SEATTLE
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    x, y = int(fx), int(fy)
    records = dm.decode_image_features(encode_tile([make_image(7, lon, lat)], x, y), x, y)
    assert len(records) == 1
    assert records[0]["lon"] == pytest.approx(lon, abs=5e-5)
    assert records[0]["lat"] == pytest.approx(lat, abs=5e-5)
    assert records[0]["captured_at_ms"] == 1650000000000
    assert records[0]["creator_id"] == 42


def test_decode_empty_or_missing_layer():
    assert (
        dm.decode_image_features(
            mapbox_vector_tile.encode([{"name": "sequence", "features": []}]), 100, 100
        )
        == []
    )


def test_decode_ids_are_strings():
    lat, lon = SEATTLE
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    x, y = int(fx), int(fy)
    records = dm.decode_image_features(
        encode_tile([make_image(1234567890123, lon, lat)], x, y), x, y
    )
    assert records[0]["id"] == "1234567890123"


# ── Capture dates ──────────────────────────────────────────────────────────


def test_captured_at_valid_epoch_ms():
    # 2022-01-03T10:43:50Z
    assert dm.captured_at_to_iso_date(1641206630491) == "2022-01-03"


@pytest.mark.parametrize(
    "bogus",
    [
        None,  # missing
        0,  # epoch zero (dead device clock)
        -1000,  # negative
        915148800000,  # 1999 — before street-level imagery existed
        4102444800000,  # year 2100 — future device clock
    ],
)
def test_captured_at_bogus_values_rejected(bogus):
    assert dm.captured_at_to_iso_date(bogus) == ""


def test_vectorized_capture_dates_match_the_scalar_rules():
    """
    The collection paths call the vectorized form over a whole census (issue
    #157), but the rules are only written out once, in the scalar function.
    Pin them together element-wise, including the values that make the two
    implementations most likely to disagree: pandas coerces out-of-range
    timestamps where datetime.fromtimestamp raises, and a null has to survive
    as '' rather than becoming 'NaT'.
    """
    values = [
        1641206630491,  # ordinary, dated
        1650000000000,
        None,  # property absent from the tile
        0,  # epoch zero (dead device clock)
        -1000,  # negative
        915148800000,  # 1999, before street-level imagery
        4102444800000,  # 2100, future device clock
        1072915199000,  # 2003-12-31, just under the 2004 floor
        1072915200000,  # 2004-01-01, the first instant above it
        9_999_999_999_999_999,  # absurd: OverflowError scalar-side
        -9_999_999_999_999_999,
    ]
    expected = [dm.captured_at_to_iso_date(v) for v in values]
    assert list(dm.captured_at_to_iso_dates(values)) == expected
    # Guard the guard: were every case '', the comparison above would pass
    # vacuously — and the 2004 floor in particular is only meaningfully tested
    # if the pair straddling it lands on opposite sides.
    assert expected[:2] == ["2022-01-03", "2022-04-15"]
    assert expected[7:9] == ["", "2004-01-01"]
    assert [e for e in expected if e == ""] == [""] * 8


def test_vectorized_capture_dates_on_an_empty_census():
    assert list(dm.captured_at_to_iso_dates([])) == []


# ── Cross-tile dedup (issue #157) ──────────────────────────────────────────


def _census_of_tile(features, x, y):
    return dm.records_to_census(dm.decode_image_features(encode_tile(features, x, y), x, y))


def test_cross_tile_dedup_keeps_the_last_copy_at_the_first_position():
    """
    The row-wise census deduped with ``images_by_id[record["id"]] = record``,
    and a dict is TWO rules: a repeated id takes the LAST copy's values but
    keeps the position of its FIRST appearance, because assigning to an
    existing key overwrites the value without reordering the key.

    ``drop_duplicates(subset="id", keep="last")`` satisfies only the first —
    it moves the surviving row to the last occurrence. Render-buffer duplicates
    are ubiquitous, so that reorders essentially every real city's run CSV, and
    the golden fixture structurally cannot catch it: its duplicate is the last
    feature of the last tile, which is the one arrangement where the two
    orderings coincide. Hence this test, on an arrangement where they don't.
    """
    x, y = 2620, 5722
    lon_a, lat_a = dm.tile_frac_to_lonlat(x + 0.25, y + 0.25, dm.TILE_ZOOM)
    lon_b, lat_b = dm.tile_frac_to_lonlat(x + 0.75, y + 0.75, dm.TILE_ZOOM)

    # "200" is published in both tiles; "100" only in the first.
    first_tile = _census_of_tile(
        [make_image(200, lon_a, lat_a), make_image(100, lon_a, lat_a)], x, y
    )
    second_tile = _census_of_tile([make_image(200, lon_b, lat_b)], x, y)
    deduped = dm.dedupe_census(dm.concat_census([first_tile, second_tile]))

    # Order: "200" stays where it FIRST appeared. keep="last" gives ["100", "200"].
    assert list(deduped["id"]) == ["200", "100"]
    # Values: the LAST copy wins. The two copies are quantized to their own
    # tile's extent, and preferring the other one can move an edge image to a
    # neighbouring grid point — a phantom change in the next run-to-run diff.
    survivor_lat = deduped.loc[0, "lat"]
    assert abs(survivor_lat - lat_b) < abs(survivor_lat - lat_a)


def test_dedup_leaves_a_duplicate_free_census_alone():
    """The no-duplicates fast path must be a genuine no-op, not a reordering."""
    x, y = 2620, 5722
    lon, lat = dm.tile_frac_to_lonlat(x + 0.5, y + 0.5, dm.TILE_ZOOM)
    census = _census_of_tile(
        [make_image(i, lon, lat) for i in (300, 100, 200)],
        x,
        y,
    )
    assert list(dm.dedupe_census(census)["id"]) == ["300", "100", "200"]


def test_a_non_integral_captured_at_does_not_cost_the_whole_tile():
    """
    Contributor-supplied tile properties are cast with pd.array(dtype="Int64"),
    which is a SAFE cast — it raises rather than coercing. That raise happens
    inside fetch_one, BEFORE any of the capture-date guards run, so an
    unguarded cast would have the tile scored as failed and every other image
    in it discarded (one z14 tile has been observed carrying 2.1M features),
    and on a small city would push the run past MAX_FAILED_TILE_FRACTION. The
    row-wise census carried such a value untouched to captured_at_to_iso_date,
    which is why that function catches OverflowError explicitly.

    A captured_at encoded as an MVT double rather than an int is the reachable
    form of this: the wire format carries doubles, and 42.5 is not a safe
    int64.
    """
    x, y = 2620, 5722
    lon, lat = dm.tile_frac_to_lonlat(x + 0.5, y + 0.5, dm.TILE_ZOOM)
    dirty = make_image(101, lon, lat)
    dirty["captured_at"] = 42.5

    census = _census_of_tile([make_image(100, lon, lat), dirty], x, y)

    # Both images survive the tile; only the unusable field is nulled.
    assert list(census["id"]) == ["100", "101"]
    assert census.loc[0, "captured_at_ms"] == 1650000000000
    assert pd.isna(census.loc[1, "captured_at_ms"])


def test_an_out_of_int64_tile_value_is_nulled_rather_than_raising():
    """
    The other reachable dirty value, applied to the decoded record rather than
    the tile: MVT carries uint64, whose top half does not fit int64 — and
    mapbox_vector_tile's own encoder refuses to emit one, so this cannot be
    built as a tile here even though a producer can publish it.
    """
    x, y = 2620, 5722
    lon, lat = dm.tile_frac_to_lonlat(x + 0.5, y + 0.5, dm.TILE_ZOOM)
    records = dm.decode_image_features(
        encode_tile([make_image(100, lon, lat), make_image(101, lon, lat)], x, y), x, y
    )
    records[1]["captured_at_ms"] = 2**64 - 1

    census = dm.records_to_census(records)

    assert list(census["id"]) == ["100", "101"]
    assert pd.isna(census.loc[1, "captured_at_ms"])


# ── Grid assignment ────────────────────────────────────────────────────────


def test_assign_grid_points_map_to_themselves():
    # Consistency between the geodesic grid builder and the equirectangular
    # assignment: every grid point's own coordinates must map back to its
    # own (i, j) index. Odd step counts exercise the asymmetric index range.
    lat, lon = SEATTLE
    step = 20
    for width_steps, height_steps in [(4, 4), (5, 5), (5, 4)]:
        points = generate_grid_points(geopy.Point(lat, lon), width_steps, height_steps, step)
        lats = np.array([p[0] for p in points])
        lons = np.array([p[1] for p in points])
        i, j, in_grid = dm.assign_to_grid(lats, lons, lat, lon, width_steps, height_steps, step)
        assert in_grid.all()
        assert list(zip(i, j, strict=False)) == [(p[2], p[3]) for p in points]


def test_assign_nearest_point_wins():
    lat, lon = SEATTLE
    step = 20
    # ~6m north and ~4m east of the center: rounds to (0, 0)
    img_lat = lat + 6 / dm._M_PER_DEG_LAT
    img_lon = lon + 4 / (dm._M_PER_DEG_LAT * math.cos(math.radians(lat)))
    i, j, in_grid = dm.assign_to_grid(
        np.array([img_lat]), np.array([img_lon]), lat, lon, 4, 4, step
    )
    assert (i[0], j[0]) == (0, 0) and in_grid[0]
    # ~14m north: rounds to (1, 0)
    img_lat = lat + 14 / dm._M_PER_DEG_LAT
    i, j, in_grid = dm.assign_to_grid(np.array([img_lat]), np.array([lon]), lat, lon, 4, 4, step)
    assert (i[0], j[0]) == (1, 0) and in_grid[0]


def test_assign_drops_images_beyond_grid_margin():
    lat, lon = SEATTLE
    step = 20
    # 4x4 steps -> i,j in [-2, 2]. Half a step beyond the edge still rounds
    # to the outermost point; a full step beyond is out of the grid.
    just_inside = lat + (2 * step + 9) / dm._M_PER_DEG_LAT
    well_outside = lat + (3 * step) / dm._M_PER_DEG_LAT
    i, _, in_grid = dm.assign_to_grid(
        np.array([just_inside, well_outside]), np.array([lon, lon]), lat, lon, 4, 4, step
    )
    assert in_grid.tolist() == [True, False]
    assert i[0] == 2


# ── End-to-end download (tiles served from memory) ─────────────────────────


@pytest.fixture
def straddling_city():
    """
    A small city grid deliberately centered on a z14 tile x-boundary so its
    bbox spans two tiles — required to exercise cross-tile dedup.
    """
    lat = SEATTLE[0]
    fx, fy = dm.lonlat_to_tile_frac(SEATTLE[1], lat, 14)
    boundary_lon, _ = dm.tile_frac_to_lonlat(int(fx), fy, 14)
    return lat, boundary_lon


def _stub_fetch_tile(monkeypatch, fetch):
    """Install ``fetch`` as the tile fetcher, keeping stubs on the plain
    ``(session, url, timeout)`` signature.

    Stubbing ``_fetch_tile`` also stubs out its retry decorator, which is what
    keeps these tests instant. But #198 moved pacing and request counting
    *inside* that function — deliberately, so a retried attempt re-paces and is
    re-counted — so a stub that ignored the limiter and counter would leave
    every city-level test seeing zero of both. This adapter honours them on the
    stub's behalf. What it cannot pin is the per-retry behaviour itself;
    ``test_a_retried_tile_re_paces_and_is_re_counted`` drives the real
    ``_fetch_tile`` for that.
    """

    async def paced(session, url, timeout, rate_limiter=None, on_request=None):
        if rate_limiter is not None:
            await rate_limiter.acquire()
        if on_request is not None:
            on_request()
        return await fetch(session, url, timeout)

    monkeypatch.setattr(dm, "_fetch_tile", paced)


def _run_download(
    monkeypatch, tmp_path, tiles_by_xy, center_lat, center_lon, width=100, height=100, step=20
):
    served = []

    async def fake_fetch(session, url, timeout):
        # The tiles CDN requires the token as a ?access_token= query param
        # (it 403s the Authorization header the Graph API uses). URL-borne
        # credentials are scrubbed from logged exception text by
        # download_common.redact_credentials (see test_credential_redaction).
        m = re.search(r"/2/14/(\d+)/(\d+)\?access_token=MLY", url)
        assert m, f"unexpected tile URL: {url}"
        assert "access_token=MLY|test|token" in url
        assert session.headers.get("Authorization") is None
        xy = (int(m.group(1)), int(m.group(2)))
        served.append(xy)
        return tiles_by_xy.get(xy, mapbox_vector_tile.encode([]))

    _stub_fetch_tile(monkeypatch, fake_fetch)
    out_path = str(tmp_path / "test_mapillary_2026-07-05.csv.gz")
    result = asyncio.run(
        dm.download_mapillary_metadata_async(
            "Test City", center_lat, center_lon, width, height, step, "MLY|test|token", out_path
        )
    )
    return result, served


def test_download_end_to_end(monkeypatch, tmp_path, straddling_city):
    lat, lon = straddling_city
    step = 20
    expected_tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 100, 100, step))
    assert len(expected_tiles) >= 2  # the straddle worked

    # One pano at the center, one ~20m east (a different grid point), one
    # flat photo at the (pano-covered) center — counted in the flat census but
    # NOT written as a FLAT_ONLY row since a pano already covers that point —
    # one pano with a dead clock (NO_DATE), and the center pano duplicated into
    # every tile (tile-buffer duplication).
    east_lon = lon + 20 / (dm._M_PER_DEG_LAT * math.cos(math.radians(lat)))
    center_pano = make_image(101, lon, lat, captured_at=1641206630491)
    east_pano = make_image(102, east_lon, lat, creator_id=7)
    flat_photo = make_image(103, lon, lat + 1e-5, is_pano=False)
    no_date = make_image(104, lon, lat + 20 / dm._M_PER_DEG_LAT, captured_at=0)

    def features_for(x, y):
        min_lon, min_lat = dm.tile_frac_to_lonlat(x, y + 1, 14)
        max_lon, max_lat = dm.tile_frac_to_lonlat(x + 1, y, 14)
        own = [
            f
            for f in (east_pano, flat_photo, no_date)
            if min_lon <= f["lon"] < max_lon and min_lat <= f["lat"] < max_lat
        ]
        return own + [center_pano]  # duplicated everywhere

    tiles_by_xy = {(x, y): encode_tile(features_for(x, y), x, y) for (x, y) in expected_tiles}
    result, served = _run_download(monkeypatch, tmp_path, tiles_by_xy, lat, lon)
    df = result["df"]

    # Contract: exact Mapillary schema (core + extras), every tile fetched once
    assert list(df.columns) == list(MAPILLARY_METADATA_DTYPES.keys())
    assert sorted(served) == sorted(expected_tiles)
    assert result["api_requests"] == len(expected_tiles)

    # Cross-tile dedup: pano 101 appears in every tile but only once here
    ok = df[df["status"] == "OK"]
    assert sorted(ok["pano_id"]) == ["101", "102"]
    assert (df["status"] == "NO_DATE").sum() == 1

    # The flat photo sits on the pano-covered center point, so it yields no
    # FLAT_ONLY row but is still tallied into the flat census (issue #116).
    assert (df["status"] == dm.FLAT_ONLY).sum() == 0
    assert result["num_flat_images"] == 1

    # Grid semantics: 100m/20m -> 6x6 grid; every point present exactly once
    # unless covered; total rows = panos + no_date + empty points
    n_points = 6 * 6
    covered = df[df["status"] != "ZERO_RESULTS"][["query_lat", "query_lon"]]
    n_covered_points = len(covered.drop_duplicates())
    assert (df["status"] == "ZERO_RESULTS").sum() == n_points - n_covered_points
    assert len(df) == 2 + 1 + (n_points - n_covered_points)

    # Panos landed on distinct nearest grid points
    assert len(ok[["query_lat", "query_lon"]].drop_duplicates()) == 2

    # Field contents
    center_row = ok[ok["pano_id"] == "101"].iloc[0]
    # the shared loader parses capture_date to Timestamp, as for GSV runs
    assert center_row["capture_date"] == pd.Timestamp("2022-01-03")
    assert center_row["copyright_info"] == "© Mapillary contributor 42"
    assert center_row["pano_lat"] == pytest.approx(lat, abs=5e-5)
    assert center_row["pano_lon"] == pytest.approx(lon, abs=5e-5)
    east_row = ok[ok["pano_id"] == "102"].iloc[0]
    assert east_row["copyright_info"] == "© Mapillary contributor 7"

    # File on disk parses through the shared loader path (result already did,
    # but verify the write is a real gzip csv)
    with gzip.open(result["filename_with_path"], "rt") as f:
        assert f.readline().strip() == ",".join(MAPILLARY_METADATA_DTYPES.keys())

    # Timestamps are ISO UTC and ordered
    started = datetime.fromisoformat(result["started_at"])
    finished = datetime.fromisoformat(result["finished_at"])
    assert started.tzinfo == UTC and started <= finished


def test_download_city_with_no_imagery(monkeypatch, tmp_path):
    # Every tile empty: pure ZERO_RESULTS fill, one row per grid point
    lat, lon = SEATTLE
    result, _ = _run_download(monkeypatch, tmp_path, {}, lat, lon)
    df = result["df"]
    assert (df["status"] == "ZERO_RESULTS").all()
    assert len(df) == 6 * 6
    assert df["pano_id"].isna().all()


def test_download_flat_only_points_get_flat_only_row(monkeypatch, tmp_path):
    # Issue #116: a grid point covered ONLY by flat imagery (no pano) becomes a
    # single FLAT_ONLY row instead of ZERO_RESULTS; a point with both a pano
    # and a flat stays a pano row (the flat only bumps the census). All
    # features sit in the single tile covering the city center.
    lat, lon = SEATTLE
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    x, y = int(fx), int(fy)
    east_lon = lon + 20 / (dm._M_PER_DEG_LAT * math.cos(math.radians(lat)))

    pano = make_image(301, lon, lat, captured_at=1641206630491)  # center point
    flat_at_pano = make_image(302, lon, lat + 1e-5, is_pano=False)  # center too
    flat_only = make_image(303, east_lon, lat, is_pano=False, creator_id=99)  # east point

    tiles = {(x, y): encode_tile([pano, flat_at_pano, flat_only], x, y)}
    result, _ = _run_download(monkeypatch, tmp_path, tiles, lat, lon)
    df = result["df"]

    # Exactly one pano row (center) and one FLAT_ONLY row (east point)
    assert sorted(df.loc[df["status"] == "OK", "pano_id"]) == ["301"]
    flat_rows = df[df["status"] == dm.FLAT_ONLY]
    assert list(flat_rows["pano_id"]) == ["303"]

    # The FLAT_ONLY row is a presence marker: it carries the representative
    # flat image's coords/copyright but a NULL capture_date (keeping flat
    # timestamps out of every dated-stat path).
    fr = flat_rows.iloc[0]
    assert pd.isna(fr["capture_date"])
    assert fr["copyright_info"] == "© Mapillary contributor 99"
    assert fr["pano_lon"] == pytest.approx(east_lon, abs=5e-5)

    # Flat census counts BOTH flats (incl. the one at the pano-covered point)
    assert result["num_flat_images"] == 2

    # Two covered points (center pano + east flat-only); the rest ZERO_RESULTS
    assert (df["status"] == "ZERO_RESULTS").sum() == 6 * 6 - 2

    # Stratified coverage: 360° counts only the pano point, any-imagery counts
    # both. Feed the run through the analysis layer to confirm.
    from streetscape_metadata_tracker.analysis import calculate_coverage_stats

    cov = calculate_coverage_stats(df)
    assert cov.num_points_with_panos == 1
    assert cov.num_points_with_any_imagery == 2
    assert cov.any_imagery_coverage_rate > cov.coverage_rate


def test_download_rejects_non_csv_gz_path(tmp_path):
    with pytest.raises(ValueError, match="csv.gz"):
        asyncio.run(
            dm.download_mapillary_metadata_async(
                "X", *SEATTLE, 100, 100, 20, "tok", str(tmp_path / "out.csv")
            )
        )


def test_run_stats_for_mapillary_have_no_google_count():
    # calculate_run_stats feeds db.register_run: for mapillary runs the
    # Google-copyright breakdown must be NULL, not zero-by-accident
    from datetime import date

    from streetscape_metadata_tracker.analysis import calculate_run_stats
    from tests.conftest import make_city_df, make_mapillary_city_df

    m_df = make_mapillary_city_df(
        [("m1", "2021-03-01"), ("m2", "2022-03-01"), ("m3", "2023-03-01")], panos_per_point=3
    )
    stats = calculate_run_stats(m_df, date(2026, 1, 15), provider="mapillary")
    assert stats["unique_panos"] == 3
    assert stats["unique_google_panos"] is None
    assert stats["status_ok"] == 3 and stats["status_zero_results"] == 1

    g_df = make_city_df([("p1", "2021-03-01")])
    g_stats = calculate_run_stats(g_df, date(2026, 1, 15))
    assert g_stats["unique_google_panos"] == 1  # gsv path unchanged


def test_download_pano_outside_grid_is_dropped(monkeypatch, tmp_path):
    # A pano inside the fetched tiles but beyond the grid margin must not
    # produce a row (the tile covers far more area than a small grid)
    lat, lon = SEATTLE
    far_lon = lon + 500 / (dm._M_PER_DEG_LAT * math.cos(math.radians(lat)))
    fx, fy = dm.lonlat_to_tile_frac(far_lon, lat, 14)
    x, y = int(fx), int(fy)
    tiles = {(x, y): encode_tile([make_image(201, far_lon, lat)], x, y)}
    result, _ = _run_download(monkeypatch, tmp_path, tiles, lat, lon)
    assert (result["df"]["status"] == "ZERO_RESULTS").all()


# ── Latitude-local grid assignment accuracy (audit 2026-07-11, M2) ──────────


def test_assign_to_grid_matches_geodesic_grid_far_from_center_at_equator():
    """A pano EXACTLY at grid point (i=120, j=0) — placed with the same
    geodesic math that builds the grid — must assign to i=120. The old
    global-mean 111,320 m/° overstated equatorial dy by ~0.67% (true
    ≈110,574 m/°), i.e. +0.8 rows at 2.4 km from center → i=121."""
    origin = geopy.Point(0.0, 30.0)
    north = geopy.distance.distance(meters=120 * 20).destination(origin, 0)
    i, j, in_grid = dm.assign_to_grid(
        np.array([north.latitude]),
        np.array([north.longitude]),
        0.0,
        30.0,
        width_steps=250,
        height_steps=250,
        step_length=20,
    )
    assert (int(i[0]), int(j[0])) == (120, 0)
    assert bool(in_grid[0])


def test_assign_to_grid_matches_geodesic_grid_far_corner_mid_latitude():
    """Same check at Seattle's latitude on a far corner point (i=120, j=120),
    exercising both the dy series and the per-row cos-latitude dx scale."""
    lat0, lon0 = 47.6, -122.3
    north = geopy.distance.distance(meters=120 * 20).destination(geopy.Point(lat0, lon0), 0)
    corner = geopy.distance.distance(meters=120 * 20).destination(north, 90)
    i, j, in_grid = dm.assign_to_grid(
        np.array([corner.latitude]),
        np.array([corner.longitude]),
        lat0,
        lon0,
        width_steps=250,
        height_steps=250,
        step_length=20,
    )
    assert (int(i[0]), int(j[0])) == (120, 120)
    assert bool(in_grid[0])


# ── Free tile extras (capture all free Mapillary metadata) ──────────────────


def test_decode_extracts_free_tile_extras():
    # The z14 image layer carries organization_id/quality_score/foot/
    # compass_angle/sequence_id per image — all pulled for zero extra requests.
    lat, lon = SEATTLE
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, 14)
    x, y = int(fx), int(fy)
    tile = encode_tile(
        [
            make_image(
                1,
                lon,
                lat,
                organization_id=518073312556755,
                quality_score=0.904,
                on_foot=True,
                compass_angle=337.1,
                sequence_id="seqA",
            ),
            make_image(2, lon + 1e-4, lat),  # individual contributor: no extras
        ],
        x,
        y,
    )
    by_id = {r["id"]: r for r in dm.decode_image_features(tile, x, y)}
    rich = by_id["1"]
    assert rich["organization_id"] == "518073312556755"  # large int coerced to str
    assert rich["quality_score"] == pytest.approx(0.904)
    assert rich["on_foot"] is True
    assert rich["compass_angle"] == pytest.approx(337.1)
    assert rich["sequence_id"] == "seqA"
    # Omitted tile properties decode to None (not an error, not a default).
    bare = by_id["2"]
    assert bare["organization_id"] is None
    assert bare["quality_score"] is None
    assert bare["on_foot"] is None
    assert bare["compass_angle"] is None
    assert bare["sequence_id"] is None


def test_extra_metadata_round_trips_to_csv(monkeypatch, tmp_path):
    # A pano carries its extras onto its row; a flat-only point carries the
    # representative flat's extras (is_pano False); an empty point nulls them.
    lat, lon = SEATTLE
    step = 20
    expected_tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 100, 100, step))
    east_lon = lon + 20 / (dm._M_PER_DEG_LAT * math.cos(math.radians(lat)))
    pano = make_image(
        201,
        lon,
        lat,
        organization_id=999,
        quality_score=0.8,
        on_foot=True,
        compass_angle=90.0,
        sequence_id="drive1",
    )
    flat = make_image(
        202,
        east_lon,
        lat,
        is_pano=False,
        quality_score=0.3,
        on_foot=False,
        sequence_id="drive2",
    )

    def features_for(x, y):
        min_lon, min_lat = dm.tile_frac_to_lonlat(x, y + 1, 14)
        max_lon, max_lat = dm.tile_frac_to_lonlat(x + 1, y, 14)
        own = [
            f for f in (flat,) if min_lon <= f["lon"] < max_lon and min_lat <= f["lat"] < max_lat
        ]
        return own + [pano]  # pano duplicated into every tile

    tiles_by_xy = {(x, y): encode_tile(features_for(x, y), x, y) for (x, y) in expected_tiles}
    result, _ = _run_download(monkeypatch, tmp_path, tiles_by_xy, lat, lon)
    df = result["df"]

    prow = df[df["pano_id"] == "201"].iloc[0]
    assert prow["organization_id"] == "999"
    assert prow["quality_score"] == pytest.approx(0.8)
    assert prow["on_foot"] == True  # noqa: E712  (nullable boolean)
    assert prow["is_pano"] == True  # noqa: E712
    assert prow["compass_angle"] == pytest.approx(90.0)
    assert prow["sequence_id"] == "drive1"
    assert prow["creator_id"] == "42"  # clean structured column, not only in copyright

    frow = df[df["status"] == dm.FLAT_ONLY].iloc[0]
    assert frow["is_pano"] == False  # noqa: E712
    assert frow["quality_score"] == pytest.approx(0.3)
    assert pd.isna(frow["organization_id"])  # flat had no org

    zrow = df[df["status"] == "ZERO_RESULTS"].iloc[0]
    for col in (
        "creator_id",
        "organization_id",
        "sequence_id",
        "is_pano",
        "on_foot",
        "quality_score",
        "compass_angle",
    ):
        assert pd.isna(zrow[col])


# ── Golden output (issue #157) ─────────────────────────────────────────────
#
# A run file is an IMMUTABLE dated snapshot, and diff.py compares one run to
# the previous one of the same series. So a purely internal change to how the
# census is assembled must not alter the written CSV: if the float formatting,
# the null rendering, or the row order shifted, every Mapillary city's next run
# would report a large phantom diff and there would be no way to tell it from
# real imagery churn.
#
# This fixture was generated from the row-wise implementation that preceded the
# columnar rewrite, and it is the contract that rewrite had to satisfy. To
# change it deliberately, run with REGEN_MAPILLARY_GOLDEN=1 and review the diff
# in tests/fixtures/mapillary_golden_run.csv as part of the change.
#
# Every field is compared byte for byte EXCEPT the two grid columns, which are
# the one part of a run CSV that is not bit-reproducible across machines:
# query_lat/query_lon come from geographiclib's geodesic solve, and libm's
# sin/cos/atan2 are not correctly rounded, so macOS and glibc disagree in the
# last ULP (~6e-15 deg — well under a nanometre; this is what reddened CI on
# the columnar PR while the same fixture passed on the laptop that made it).
# Production is already immune: diff.py keys grid points at its own
# _COORD_DECIMALS = 6, i.e. ~11 cm, so a difference this small is invisible to
# the very diff this fixture exists to protect. The gate below is still about
# four orders of magnitude tighter than the coarsest regression it must catch
# (a float32 cast, which at this latitude lands ~6e-6 deg off).

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "mapillary_golden_run.csv"
# Pinned so the fixture never depends on the wall clock. Every row carries the
# run's query_timestamp, which is datetime.now() at call time.
GOLDEN_TIMESTAMP_PLACEHOLDER = "<QUERY_TIMESTAMP>"
_GRID_COORD_TOLERANCE_DEG = 1e-9


def _assert_csv_matches_golden(written: str, golden: str) -> None:
    """
    Compare a written run CSV to the golden fixture line by line, exactly
    except for the platform-dependent last ULP of the grid coordinates.

    Deliberately not ``pd.read_csv`` + ``assert_frame_equal``: parsing would
    discard precisely what this fixture exists to pin — the float repr, the
    empty-vs-NaN rendering, the column order, and the row order.
    """
    written_lines, golden_lines = written.splitlines(), golden.splitlines()
    drift = (
        "the written run CSV changed. If that is deliberate, regenerate with "
        "REGEN_MAPILLARY_GOLDEN=1 and review the fixture diff — but note that "
        "shipping it makes every Mapillary city's next run-to-run diff report "
        "changes that did not happen."
    )
    assert len(written_lines) == len(golden_lines), f"{drift} (row count)"
    assert written_lines[0] == golden_lines[0], f"{drift} (header)"

    header = golden_lines[0].split(",")
    # Safe as positional indices even though a later field could be quoted and
    # contain a comma: the grid columns lead every row.
    grid_columns = (header.index("query_lat"), header.index("query_lon"))
    assert grid_columns == (0, 1)

    for n, (written_line, golden_line) in enumerate(
        zip(written_lines[1:], golden_lines[1:], strict=True), start=2
    ):
        written_fields, golden_fields = written_line.split(","), golden_line.split(",")
        assert len(written_fields) == len(golden_fields), f"{drift} (line {n}: field count)"
        for column in grid_columns:
            delta = abs(float(written_fields[column]) - float(golden_fields[column]))
            assert delta < _GRID_COORD_TOLERANCE_DEG, (
                f"{drift} (line {n}: {header[column]} moved by {delta:g} deg, which is "
                "too far to be the cross-platform geodesic noise this tolerance allows)"
            )
        assert [f for k, f in enumerate(written_fields) if k not in grid_columns] == [
            f for k, f in enumerate(golden_fields) if k not in grid_columns
        ], f"{drift} (line {n})"


def _golden_features(lat, lon):
    """
    A census exercising every branch of the grid downloader's row assembly.

    Returns (features_by_tile_role, expectations) where the first element maps
    a role to the images that tile should carry — "all" goes into every tile,
    "first"/"last" into the first and last tile of the bbox respectively (the
    city straddles a tile boundary, so both cover the center longitude).
    """
    m_lat = dm._M_PER_DEG_LAT
    m_lon = m_lat * math.cos(math.radians(lat))

    def at(north_m, east_m):
        return lon + east_m / m_lon, lat + north_m / m_lat

    def img(image_id, north_m, east_m, **kw):
        image_lon, image_lat = at(north_m, east_m)
        return make_image(image_id, image_lon, image_lat, **kw)

    everywhere = [
        # ── panos, one per capture-date branch ──
        # a fully-populated pano: OK, and every free tile extra present
        img(
            101,
            0,
            0,
            captured_at=1641206630491,
            creator_id=42,
            organization_id=999,
            quality_score=0.8,
            on_foot=True,
            compass_angle=90.0,
            sequence_id="drive1",
        ),
        # a SECOND pano on the same grid point — Mapillary is a census, so both
        # get rows (this is what makes pano counts census-vs-sample). creator
        # None also exercises the bare "© Mapillary" copyright form.
        img(102, 3, 0, captured_at=1650000000000, creator_id=None),
        # the four ways a capture date is unusable -> NO_DATE with an empty date
        img(103, 20, 0, captured_at=0),  # epoch zero (dead device clock)
        img(104, 40, 0, captured_at=None),  # property absent from the tile
        img(105, 60, 0, captured_at=1000000000000),  # 2001, before Mapillary
        img(106, 80, 0, captured_at=32503680000000),  # year 3000, in the future
        # ── flats (issue #116) ──
        # a flat at a pano-covered point: counted in the flat census magnitude,
        # but NOT written as a row (the pano already covers that point)
        img(107, 2, 0, is_pano=False),
        # a flat at a point with no pano -> one FLAT_ONLY marker row, null date
        img(108, 0, 20, is_pano=False, quality_score=0.3, sequence_id="walk1"),
        # a second flat on that same point: the FIRST stays the representative
        img(109, 3, 20, is_pano=False, quality_score=0.9),
        # ── out of grid (the grid is +/-100 m) -> dropped entirely ──
        img(110, 500, 0),
        img(111, -500, 0, is_pano=False),
    ]
    # Same id in two tiles at very different positions. Real duplicates come
    # from the render buffer and differ only by tile quantization, but the
    # dedup rule still has to be pinned: last tile wins, so this lands SOUTH.
    first_only = [img(112, 60, 0, captured_at=1650000000000)]
    last_only = [img(112, -60, 0, captured_at=1650000000000)]
    return everywhere, first_only, last_only


def test_written_csv_matches_the_golden_fixture(monkeypatch, tmp_path, straddling_city):
    lat, lon = straddling_city
    tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 200, 200, 20))
    assert len(tiles) >= 2, "the straddle must span tiles for the dedup case to mean anything"

    everywhere, first_only, last_only = _golden_features(lat, lon)
    tiles_by_xy = {
        (x, y): encode_tile(
            everywhere
            + (first_only if (x, y) == tiles[0] else [])
            + (last_only if (x, y) == tiles[-1] else []),
            x,
            y,
        )
        for (x, y) in tiles
    }

    result, _ = _run_download(
        monkeypatch, tmp_path, tiles_by_xy, lat, lon, width=200, height=200, step=20
    )
    with gzip.open(result["filename_with_path"], "rt", encoding="utf-8") as f:
        written = f.read()
    # The one genuinely non-deterministic field: pin it rather than exclude it,
    # so a change in how the timestamp is rendered still fails this test.
    assert result["started_at"] in written
    written = written.replace(result["started_at"], GOLDEN_TIMESTAMP_PLACEHOLDER)

    if os.environ.get("REGEN_MAPILLARY_GOLDEN"):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(written, encoding="utf-8")
        pytest.skip(f"regenerated {GOLDEN_PATH}")

    _assert_csv_matches_golden(written, GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_fixture_covers_every_status_and_the_dedup_rule(straddling_city):
    """
    Guards the fixture itself: a golden file only protects the branches it
    actually exercises, and a silently-narrowed fixture would still pass the
    byte comparison above while protecting nothing.
    """
    lat, _ = straddling_city
    golden = pd.read_csv(GOLDEN_PATH, dtype=str, keep_default_na=False)

    assert set(golden["status"]) == {"OK", "NO_DATE", dm.FLAT_ONLY, "ZERO_RESULTS"}
    # One row per grid point on an 11x11 grid, plus one extra for the second
    # pano sharing the center point — that census-vs-sample duplication is
    # exactly what the row assembly must not collapse.
    assert len(golden) == 121 + 1
    assert (golden[["query_lat", "query_lon"]].drop_duplicates().shape[0]) == 121
    # Both copyright forms, and a dated pano alongside the undated ones.
    assert {"© Mapillary contributor 42", "© Mapillary"} <= set(golden["copyright_info"])
    assert (golden["capture_date"] == "2022-01-03").sum() == 1
    assert (golden["status"] == "NO_DATE").sum() == 4
    # FLAT_ONLY carries the FIRST flat at that point and never a capture date.
    flat_only = golden[golden["status"] == dm.FLAT_ONLY]
    assert list(flat_only["pano_id"]) == ["108"]
    assert list(flat_only["capture_date"]) == [""]
    # Last tile wins the duplicate id: 112 landed south of center, not north.
    dup = golden[golden["pano_id"] == "112"].iloc[0]
    assert float(dup["pano_lat"]) < lat


def test_golden_comparison_tolerates_only_cross_platform_grid_noise():
    """
    Guards the comparison itself. Relaxing the grid columns off an exact byte
    match is what let this fixture run on both macOS and glibc, and the way
    that goes wrong is silently: a tolerance wide enough to swallow a real
    regression leaves a golden test that passes no matter what ships.
    """
    golden = GOLDEN_PATH.read_text(encoding="utf-8")
    lines = golden.splitlines()

    def perturb(line, column, delta):
        fields = line.split(",")
        fields[column] = repr(float(fields[column]) + delta)
        return ",".join(fields)

    def nudge(line, column, ulps):
        """Move a coordinate by whole ULPs AT ITS OWN MAGNITUDE."""
        value = float(line.split(",")[column])
        return perturb(line, column, ulps * math.ulp(value))

    def mutated(new_lines):
        return "\n".join(new_lines) + "\n"

    # Accepted: last-ULP drift in BOTH grid columns on every row, which is the
    # real macOS-vs-glibc geodesic difference (~7e-15 deg) this exists for.
    nudged = [lines[0]] + [nudge(nudge(line, 0, 1), 1, -1) for line in lines[1:]]
    # The nudge has to be magnitude-relative, and this asserts that it was: a
    # fixed 7e-15 is a whole ULP at latitude ~47 but BELOW HALF a ULP at
    # longitude ~122 (ULP 1.42e-14), where it rounds straight back to the
    # original repr. Measured on this fixture, it moved 122 of 122 query_lat
    # values and 0 of 122 query_lon values — so the acceptance half of this
    # test demonstrated nothing whatsoever about query_lon, and a comparison
    # that had silently gone back to requiring exact bytes there would have
    # passed it. That is the same "passes no matter what ships" failure this
    # test exists to prevent, one level up.
    for column in (0, 1):
        assert all(
            n.split(",")[column] != g.split(",")[column]
            for n, g in zip(nudged[1:], lines[1:], strict=True)
        ), f"the nudge never moved column {column}, so its tolerance is untested here"
    _assert_csv_matches_golden(mutated(nudged), golden)

    # Rejected: a grid point that actually moved. 6e-6 deg is where a float32
    # cast of these coordinates lands — the coarsest thing the tolerance has
    # to catch, and still far below diff.py's 11 cm keying.
    with pytest.raises(AssertionError, match="query_lat moved by"):
        _assert_csv_matches_golden(
            mutated([lines[0], perturb(lines[1], 0, 6e-6), *lines[2:]]), golden
        )

    # Rejected: everything the fixture is actually for — row order, image
    # coordinates (which are exact everywhere; only the GRID math is fuzzy),
    # a dropped row, a changed status, and a null rendered differently.
    reordered = [lines[0], lines[2], lines[1], *lines[3:]]
    with pytest.raises(AssertionError):
        _assert_csv_matches_golden(mutated(reordered), golden)

    with pytest.raises(AssertionError):
        _assert_csv_matches_golden(
            mutated([lines[0], perturb(lines[1], 3, 1e-12), *lines[2:]]), golden
        )

    with pytest.raises(AssertionError, match="row count"):
        _assert_csv_matches_golden(mutated([lines[0], *lines[2:]]), golden)

    with pytest.raises(AssertionError):
        _assert_csv_matches_golden(
            mutated([line.replace("NO_DATE", "OK") for line in lines]), golden
        )

    with pytest.raises(AssertionError):
        _assert_csv_matches_golden(golden.replace(",,", ",nan,"), golden)


def test_compute_mapillary_meta_summary():
    cols = list(MAPILLARY_METADATA_DTYPES.keys())

    def row(status, org, foot, q, is_pano=True):
        d = dict.fromkeys(cols)
        d.update(
            query_lat=0.0,
            query_lon=0.0,
            query_timestamp="2026-07-23T00:00:00+00:00",
            status=status,
            organization_id=org,
            on_foot=foot,
            quality_score=q,
            is_pano=is_pano,
        )
        return d

    rows = [
        row("OK", "111", True, 0.9),
        row("OK", "111", False, 0.7),
        row("NO_DATE", None, True, 0.5),
        row("OK", "222", None, None),
        row("ZERO_RESULTS", None, None, None, is_pano=None),  # excluded (no image)
        row("FLAT_ONLY", "333", False, 0.2, is_pano=False),  # excluded (not a pano)
    ]
    df = pd.DataFrame(rows, columns=cols).astype(MAPILLARY_METADATA_DTYPES)
    meta = compute_mapillary_meta(df)
    assert meta["n_images"] == 4  # 3 OK + 1 NO_DATE
    assert meta["n_distinct_orgs"] == 2  # 111, 222
    assert meta["pct_with_org"] == 75.0  # 3 of 4 panos attributed to an org
    assert meta["pct_on_foot"] == pytest.approx(66.7)  # 2 True of 3 known
    assert meta["median_quality_score"] == pytest.approx(0.7)  # median of [0.9,0.7,0.5]


def test_compute_mapillary_meta_none_for_legacy_schema():
    # A pre-enrichment Mapillary file (core columns only) yields no block.
    from streetscape_metadata_tracker.config import METADATA_DTYPES

    df = pd.DataFrame(
        [dict.fromkeys(METADATA_DTYPES, None) | {"status": "OK"}],
        columns=list(METADATA_DTYPES.keys()),
    )
    assert compute_mapillary_meta(df) is None


# ── Tile fault tolerance (issue #168) ──────────────────────────────────────


def _failing_fetch(fail_xy, tiles_by_xy=None, status=404):
    """A _fetch_tile stand-in where one chosen tile always errors."""

    async def fake_fetch(session, url, timeout):
        m = re.search(r"/2/14/(\d+)/(\d+)\?", url)
        xy = (int(m.group(1)), int(m.group(2)))
        if xy in fail_xy:
            # A real RequestInfo: aiohttp's __str__ dereferences it, and the
            # error text goes through redact_credentials, so the URL (which
            # carries the access token) must be present and scrubbable.
            request_info = aiohttp.RequestInfo(
                url=yarl.URL(url),
                method="GET",
                headers=CIMultiDictProxy(CIMultiDict()),
                real_url=yarl.URL(url),
            )
            raise aiohttp.ClientResponseError(
                request_info=request_info, history=(), status=status, message="Not Found"
            )
        return (tiles_by_xy or {}).get(xy, mapbox_vector_tile.encode([]))

    return fake_fetch


def _fetch_city(monkeypatch, fetch, lat, lon, width=30000, height=30000, step=2000, **kwargs):
    """Run a whole-city fetch with ``fetch`` standing in for one tile request.

    ``kwargs`` reach ``fetch_city_images_async`` verbatim, so a test can pin a
    non-default pacing argument all the way to the limiter rather than only
    pinning the module default.
    """
    _stub_fetch_tile(monkeypatch, fetch)
    return asyncio.run(
        dm.fetch_city_images_async(
            "Test City", dm.grid_bbox(lat, lon, width, height, step), "MLY|test|token", **kwargs
        )
    )


def test_one_bad_tile_no_longer_kills_the_whole_city(monkeypatch):
    """The regression this exists for: Chicago 2026-07-29 lost both Mapillary
    channels to a single transient 404 on z14/4196/6084, and the same tile
    served 2.1M features the next day."""
    lat, lon = 41.8, -87.7
    all_tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 30000, 30000, 2000))
    assert 1 / len(all_tiles) <= dm.MAX_FAILED_TILE_FRACTION, "one tile must be under threshold"

    result = _fetch_city(monkeypatch, _failing_fetch({all_tiles[0]}), lat, lon)

    assert result["failed_tiles"] == [all_tiles[0]]
    assert result["tiles"] == len(all_tiles), "the tile set is still reported in full"


def test_too_many_failed_tiles_still_refuses_to_finalize(monkeypatch):
    """Tolerating a blip must not mean tolerating a hole."""
    lat, lon = 41.8, -87.7
    all_tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 30000, 30000, 2000))

    with pytest.raises(DownloadError) as excinfo:
        _fetch_city(monkeypatch, _failing_fetch(set(all_tiles)), lat, lon)

    assert "refusing to finalize" in str(excinfo.value)
    # The ledger still learns what the doomed attempt spent.
    assert getattr(excinfo.value, "api_requests", 0) > 0


def test_a_rejected_token_is_never_treated_as_a_partial_failure(monkeypatch):
    """401/403 is a whole-city condition — every other tile would fail the same
    way, so it must not be dressed up as one tolerable bad tile."""
    lat, lon = 41.8, -87.7

    async def bad_token(session, url, timeout):
        raise DownloadError("Mapillary rejected the access token (HTTP 401).")

    with pytest.raises(DownloadError) as excinfo:
        _fetch_city(monkeypatch, bad_token, lat, lon)

    assert "rejected the access token" in str(excinfo.value)
    assert "refusing to finalize" not in str(excinfo.value)


def test_points_under_a_failed_tile_are_request_failed_not_empty(monkeypatch, tmp_path):
    """An uncovered point under an undownloaded tile is UNKNOWN. Left as
    ZERO_RESULTS it is indistinguishable from genuine no-imagery and quietly
    understates coverage."""
    lat, lon = 41.8, -87.7
    width = height = 30000
    step = 500  # coarse: many tiles (so one failure is tolerated), few points
    all_tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, width, height, step))
    failed = all_tiles[len(all_tiles) // 2]  # an interior tile, not a corner

    _stub_fetch_tile(monkeypatch, _failing_fetch({failed}))
    out_path = str(tmp_path / "test_mapillary_2026-07-05.csv.gz")
    result = asyncio.run(
        dm.download_mapillary_metadata_async(
            "Test City", lat, lon, width, height, step, "MLY|test|token", out_path
        )
    )

    df = result["df"]
    failed_rows = df[df["status"] == "REQUEST_FAILED"]
    assert len(failed_rows) > 0, "the failed tile covers part of this grid"

    # Every REQUEST_FAILED row really does sit inside the failed tile...
    inside = dm._points_in_tiles(
        failed_rows["query_lat"].to_numpy(), failed_rows["query_lon"].to_numpy(), [failed]
    )
    assert inside.all()
    # ...and no point outside it was mislabelled.
    empty_rows = df[df["status"] == "ZERO_RESULTS"]
    outside = dm._points_in_tiles(
        empty_rows["query_lat"].to_numpy(), empty_rows["query_lon"].to_numpy(), [failed]
    )
    assert not outside.any()


def test_a_clean_run_reports_no_failed_tiles(monkeypatch, straddling_city):
    """The tolerance path must be inert when nothing goes wrong."""
    lat, lon = straddling_city
    result = _fetch_city(monkeypatch, _failing_fetch(set()), lat, lon)
    assert result["failed_tiles"] == []


# ── Tile-CDN blocks are not tile corruption (issue #199) ───────────────────
#
# On 2026-08-12 a bulk collection sustained ~370 tile requests/min and got the
# host's IP blocked: every tile request 302'd to www.mapillary.com/login/.
# aiohttp followed the redirect, the login page returned a perfectly good HTTP
# 200, and its HTML body reached mapbox_vector_tile.decode() — so a banned host
# surfaced as `DecodeError: Error parsing message with type 'vector_tile.tile'`,
# indistinguishable from #168's transient bad tile. These pin the diagnosis.

# The real block page, verbatim: "尚未登录 / 请登录查看这一页面" ("Not logged in /
# Please log in to view this page"). Meta serves it localized to unauthenticated
# requests; the language carries no meaning here.
_LOGIN_PAGE_BODY = (
    b"<h1>\xe5\xb0\x9a\xe6\x9c\xaa\xe7\x99\xbb\xe5\xbd\x95</h1>"
    b"<p>\xe8\xaf\xb7\xe7\x99\xbb\xe5\xbd\x95\xe6\x9f\xa5\xe7\x9c\x8b"
    b"\xe8\xbf\x99\xe4\xb8\x80\xe9\xa1\xb5\xe9\x9d\xa2\xe3\x80\x82</p>"
)

_TILE_URL = (
    "https://tiles.mapillary.com/maps/vtp/mly1_public/2/14/4196/6084?access_token=MLY|s3cret"
)


class _FakeTileResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    def raise_for_status(self):
        """As aiohttp does it — without this the fake cannot reach the 4xx/5xx
        path at all, and a test aimed at the block checks fails with an
        AttributeError instead of its own assertion."""
        if self.status >= 400:
            request_info = aiohttp.RequestInfo(
                url=yarl.URL(_TILE_URL),
                method="GET",
                headers=CIMultiDictProxy(CIMultiDict()),
                real_url=yarl.URL(_TILE_URL),
            )
            raise aiohttp.ClientResponseError(
                request_info=request_info, history=(), status=self.status, message="error"
            )


class _FakeTileSession:
    """Minimal aiohttp.ClientSession stand-in: .get() as an async CM."""

    def __init__(self, response):
        self._response = response
        self.get_kwargs = []

    def get(self, url, **kwargs):
        self.get_kwargs.append(kwargs)
        response = self._response

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _fetch_tile(response):
    session = _FakeTileSession(response)
    with pytest.raises(DownloadError) as excinfo:
        asyncio.run(_fetch_tile_coro(session))
    return session, excinfo.value


def _fetch_tile_coro(session):
    return dm._fetch_tile(session, _TILE_URL, aiohttp.ClientTimeout(total=5))


def test_tile_requests_do_not_follow_redirects():
    """The fix hinges on seeing the 302 itself: if aiohttp follows it, the login
    page's own HTTP 200 is what the status checks see."""
    session = _FakeTileSession(_FakeTileResponse(200, {"Content-Type": "application/x-protobuf"}))
    asyncio.run(_fetch_tile_coro(session))
    assert session.get_kwargs[0]["allow_redirects"] is False


def test_a_login_redirect_names_the_ip_rate_limit():
    _, error = _fetch_tile(
        _FakeTileResponse(
            302,
            {"Location": "https://www.mapillary.com/login/?next=" + _TILE_URL},
        )
    )
    text = str(error)
    assert "rate-limited" in text
    assert "login" in text.lower()
    # The old symptom must not be how this reads any more.
    assert "parsing message" not in text


@pytest.mark.parametrize(
    "next_param",
    [
        _TILE_URL,
        # The shape a real Location takes: the request URL percent-encoded into
        # the login page's own query string. `access_token=` becomes
        # `access_token%3D`, which the pre-#199 redaction pattern did not match
        # at all — so the token travelled to the logs in full.
        urllib.parse.quote(_TILE_URL, safe=""),
        urllib.parse.quote_plus(_TILE_URL),
    ],
    ids=["unencoded", "quoted", "quoted_plus"],
)
def test_a_login_redirect_does_not_leak_the_token(next_param):
    """Location echoes the request URL, token and all, and this text reaches
    logs and the scheduler's alert emails."""
    _, error = _fetch_tile(
        _FakeTileResponse(302, {"Location": "https://www.mapillary.com/login/?next=" + next_param})
    )
    assert "s3cret" not in str(error)
    assert "REDACTED" in str(error)


def test_an_html_error_page_is_not_fed_to_the_protobuf_decoder():
    """The 200-with-HTML case, i.e. what we would have seen had the redirect
    been followed for us by something upstream."""
    _, error = _fetch_tile(
        _FakeTileResponse(200, {"Content-Type": 'text/html; charset="utf-8"'}, _LOGIN_PAGE_BODY)
    )
    assert "error page instead of a vector tile" in str(error)
    assert "rate limit" in str(error)


def test_a_5xx_tile_is_left_to_the_retry_layer():
    """The new checks must not swallow the transient path. A 429/5xx is still
    a ClientResponseError for backoff to retry and, past that, one tolerable
    bad tile under MAX_FAILED_TILE_FRACTION (issue #168) — NOT a DownloadError,
    which would fail the whole city. Called through __wrapped__ to skip the
    retry sleeps."""
    session = _FakeTileSession(_FakeTileResponse(503))
    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(dm._fetch_tile.__wrapped__(session, _TILE_URL, aiohttp.ClientTimeout(total=5)))


def test_an_unlabelled_tile_is_still_accepted():
    """A deny-list, not an allow-list: if Mapillary relabels real tiles, an
    allow-list would reject every tile and halt collection everywhere."""
    body = mapbox_vector_tile.encode([])
    session = _FakeTileSession(
        _FakeTileResponse(200, {"Content-Type": "application/vnd.mapbox-vector-tile"}, body)
    )
    assert asyncio.run(_fetch_tile_coro(session)) == body


def test_a_blocked_host_fails_the_city_by_name_not_as_a_partial_snapshot(monkeypatch):
    """Like a rejected token, a block is a whole-city condition — every
    remaining tile fails identically, so it must not be dressed up as tolerable
    partial coverage."""
    lat, lon = 41.8, -87.7

    async def blocked(session, url, timeout):
        raise DownloadError(
            "Mapillary tile CDN redirected to a login page (HTTP 302 → "
            "https://www.mapillary.com/login/?next=REDACTED). This host's IP is "
            "likely rate-limited on tiles.mapillary.com"
        )

    with pytest.raises(DownloadError) as excinfo:
        _fetch_city(monkeypatch, blocked, lat, lon)

    assert "rate-limited" in str(excinfo.value)
    assert "refusing to finalize" not in str(excinfo.value)


# ── Tile-CDN pacing (issue #198) ───────────────────────────────────────────


def test_every_tile_request_passes_through_the_rate_limiter(monkeypatch, straddling_city):
    """The prevention half of the 2026-08-12 ban: before this, nothing bounded
    the aggregate rate — connection_limit caps concurrency, which on a fast link
    still meant ~5 tiles/s from a single city."""
    lat, lon = straddling_city
    acquires = []

    class _SpyLimiter:
        def __init__(self, max_per_minute, *args, **kwargs):
            self.max_per_minute = max_per_minute

        async def acquire(self):
            acquires.append(self.max_per_minute)

    monkeypatch.setattr(dm, "AsyncRateLimiter", _SpyLimiter)
    result = _fetch_city(monkeypatch, _failing_fetch(set()), lat, lon)

    assert acquires, "no tile request was paced"
    assert len(acquires) == result["api_requests"]
    assert set(acquires) == {dm.DEFAULT_TILE_REQUESTS_PER_MINUTE}


def test_the_default_pace_is_well_under_the_rate_that_got_us_banned():
    """370/min is confirmed too high (2026-08-12). The default is a guess, but
    it must stay a conservative one — this is the guard on someone 'tuning' it
    up without new evidence about where the real ceiling is."""
    assert 0 < dm.DEFAULT_TILE_REQUESTS_PER_MINUTE <= 120


def test_the_tile_limiter_is_built_jittered(monkeypatch, straddling_city):
    """Issue #292: the census builds its limiter with the jitter it was given,
    and the module default is non-zero — a saturated token bucket's exact
    cadence is the one property of our traffic three blocks never changed.

    Both halves matter, and the second is the one a green suite missed before:
    pinning only the default lets the construction site hardcode
    ``DEFAULT_TILE_JITTER`` and ignore its argument, which would silently
    disable ``--mapillary-jitter 0`` — this experiment's CONTROL arm — and every
    per-channel config override with it. The rate is pinned the same way for the
    same reason.
    """
    lat, lon = straddling_city
    built = []

    class _Recording:
        def __init__(self, max_per_minute, *args, **kwargs):
            built.append((max_per_minute, kwargs.get("jitter")))

        async def acquire(self):
            return None

    monkeypatch.setattr(dm, "AsyncRateLimiter", _Recording)

    _fetch_city(monkeypatch, _failing_fetch(set()), lat, lon)
    assert built == [(dm.DEFAULT_TILE_REQUESTS_PER_MINUTE, dm.DEFAULT_TILE_JITTER)]
    assert 0 < dm.DEFAULT_TILE_JITTER < 1

    # ...and a caller's own values, including the metronome the control arm asks
    # for, must survive the trip instead of being replaced by those defaults.
    built.clear()
    _fetch_city(monkeypatch, _failing_fetch(set()), lat, lon, jitter=0.0, max_requests_per_minute=7)
    assert built == [(7, 0.0)]


class _SequencedTileSession:
    """A session whose .get() serves a scripted outcome per attempt.

    Distinct from _FakeTileSession, which replays one response forever: the
    point here is that attempt 1 and attempt 3 differ, so a retry is
    observable.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.attempts = 0

    def get(self, url, **kwargs):
        self.attempts += 1
        outcome = self._outcomes.pop(0)

        class _Ctx:
            async def __aenter__(self):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def test_a_retried_tile_re_paces_and_is_re_counted():
    """One token per HTTP request, not per tile.

    _fetch_tile may issue up to _TILE_MAX_TRIES requests. Pacing in the caller
    bought one token for all of them, so a retrying tile could present five
    times the configured rate — during a 429/5xx storm, i.e. exactly when the
    CDN is least willing to absorb it — and report a fifth of its true spend to
    the api_usage ledger.
    """
    tile = mapbox_vector_tile.encode([])
    session = _SequencedTileSession(
        [
            aiohttp.ClientConnectionError("connection reset"),
            aiohttp.ClientConnectionError("connection reset"),
            _FakeTileResponse(200, {"Content-Type": "application/x-protobuf"}, tile),
        ]
    )
    acquires, counted = [], []

    class _SpyLimiter:
        async def acquire(self):
            acquires.append(1)

    body = asyncio.run(
        dm._fetch_tile(
            session,
            _TILE_URL,
            aiohttp.ClientTimeout(total=5),
            _SpyLimiter(),
            lambda: counted.append(1),
        )
    )

    assert body == tile
    assert session.attempts == 3, "the fixture must actually exercise a retry"
    assert len(acquires) == session.attempts
    assert len(counted) == session.attempts


# ---------------------------------------------------------------------------
# Fail fast on a whole-host condition (issue #205)
#
# The block/token/error-page classes are whole-CITY conditions, but gather()
# used to settle every tile before the settle loop could re-raise — so a blocked
# host spent its complete tile count at the paced 60/min to learn what the first
# response already said. Fresno: 210 requests, 3.5 min, twice a night.
#
# Pacing is disabled suite-wide by conftest, so these assert on REQUEST COUNTS,
# never on elapsed time.
# ---------------------------------------------------------------------------


_ALL_TILES = len(dm.tiles_for_bbox(*dm.grid_bbox(41.8, -87.7, 30000, 30000, 2000)))
# Read from the signature rather than copied: this bounds what a blocked host
# costs, so a hand-written 5 would silently get looser (or wrongly tight) the
# day the default changes, which is exactly when the bound needs checking.
_CONNECTION_LIMIT = inspect.signature(dm._fetch_city_images).parameters["connection_limit"].default


def _counting_fatal_fetch(error):
    """A tile fetcher that records every request and always fails fatally."""
    served = []

    async def fetch(session, url, timeout):
        served.append(url)
        raise error()

    return fetch, served


def test_a_blocked_host_stops_instead_of_paying_for_every_tile(monkeypatch):
    """The headline fix: learn it once, not 361 times."""
    fetch, served = _counting_fatal_fetch(
        lambda: HostBlockedError(
            "Mapillary tile CDN redirected instead of serving a tile (HTTP 302 → "
            "https://www.mapillary.com/login/?next=REDACTED). ... rate-limited ...",
            host=HOST_MAPILLARY_TILES,
        )
    )

    with pytest.raises(HostBlockedError) as excinfo:
        _fetch_city(monkeypatch, fetch, 41.8, -87.7)

    assert len(served) <= _CONNECTION_LIMIT, (
        f"a blocked host must stop after the in-flight tiles drain, not issue all {_ALL_TILES}"
    )
    assert len(served) < _ALL_TILES
    # The ledger must agree with what was actually issued, or the budget guard
    # starts working from fiction.
    assert excinfo.value.api_requests == len(served)


def test_a_rejected_token_also_stops_early(monkeypatch):
    """Same shape, different cause: every remaining tile carries the same key."""
    fetch, served = _counting_fatal_fetch(
        lambda: DownloadError("Mapillary rejected the access token (HTTP 401).")
    )

    with pytest.raises(DownloadError):
        _fetch_city(monkeypatch, fetch, 41.8, -87.7)

    assert len(served) <= _CONNECTION_LIMIT


def test_one_bad_tile_still_fans_out_to_every_tile(monkeypatch):
    """
    The #168 regression guard, and the reason the abort keys on DownloadError
    alone. A transient per-tile 404 is worth one tile, not a city — it must NOT
    trip the early abort, or a single flaky tile would truncate the census and
    still "succeed" under MAX_FAILED_TILE_FRACTION.
    """
    served = []
    inner = _failing_fetch({(0, 0)})  # a tile that is never in this bbox

    async def counting(session, url, timeout):
        served.append(url)
        return await inner(session, url, timeout)

    result = _fetch_city(monkeypatch, counting, 41.8, -87.7)

    assert len(served) == _ALL_TILES, "every tile must still be requested"
    assert result["api_requests"] == _ALL_TILES


def test_a_tolerated_per_tile_failure_does_not_truncate_the_city(monkeypatch):
    """One genuinely failing tile, under the 2% tolerance: the other 360 are
    still fetched rather than being skipped by an over-eager abort."""
    tiles = dm.tiles_for_bbox(*dm.grid_bbox(41.8, -87.7, 30000, 30000, 2000))
    served = []
    inner = _failing_fetch({tiles[0]})

    async def counting(session, url, timeout):
        served.append(url)
        return await inner(session, url, timeout)

    result = _fetch_city(monkeypatch, counting, 41.8, -87.7)

    assert len(served) == _ALL_TILES
    assert result["failed_tiles"] == [tiles[0]]


def test_a_host_block_is_typed_but_a_bad_token_is_not():
    """
    A block belongs to the IP; a rejected token belongs to the credential, and
    our two Mapillary channels hold DIFFERENT tokens. Typing 401 host-wide would
    let one channel's bad key stop the other, which is working fine.
    """
    _, blocked = _fetch_tile(
        _FakeTileResponse(302, {"Location": "https://www.mapillary.com/login/?next=x"})
    )
    assert isinstance(blocked, HostBlockedError)
    assert blocked.host == HOST_MAPILLARY_TILES

    _, rejected = _fetch_tile(_FakeTileResponse(401, {}))
    assert isinstance(rejected, DownloadError)
    assert not isinstance(rejected, HostUnavailableError)


def test_an_html_error_page_is_also_a_host_block():
    """The pre-#199 symptom: HTTP 200 whose body is a login page. Same host
    condition as the 302, so it must carry the same type."""
    _, error = _fetch_tile(_FakeTileResponse(200, {"Content-Type": "text/html"}, b"<html>"))
    assert isinstance(error, HostBlockedError)
    assert error.host == HOST_MAPILLARY_TILES


def test_a_swallowed_fatal_error_can_never_publish_an_empty_census(monkeypatch):
    """
    The invariant behind the fail-fast, made structural rather than emergent.

    Aborted tiles return an EMPTY CENSUS, which the settle loop reads as a
    successful tile. Today that is safe because the task that set `fatal` also
    re-raised, so its exception is in `settled`. If a later edit makes
    `fetch_one` swallow or wrap that error, nothing else would object: no tile
    is in `failed_tiles`, `detect_systemic_failure` only looks for
    REQUEST_DENIED/OVER_QUERY_LIMIT, and a 0-pano census would register,
    publish, and diff as "every pano in the city removed" — against an
    immutable dated snapshot.

    Simulated at the seam rather than by editing fetch_one: drop the exceptions
    out of `settled` after the gather, which is precisely the state such an edit
    would leave behind — every tile a clean, empty success.
    """
    served = []

    async def blocked(session, url, timeout):
        served.append(url)
        raise HostBlockedError("tile CDN redirected", host=HOST_MAPILLARY_TILES)

    real_gather = asyncio.gather

    async def gather_swallowing_exceptions(*aws, **kwargs):
        settled = await real_gather(*aws, **kwargs)
        return [dm.records_to_census([]) if isinstance(s, BaseException) else s for s in settled]

    monkeypatch.setattr(asyncio, "gather", gather_swallowing_exceptions)

    with pytest.raises(HostBlockedError) as excinfo:
        _fetch_city(monkeypatch, blocked, 41.8, -87.7)

    assert excinfo.value.host == HOST_MAPILLARY_TILES
    # Still the error that caused the abort, and still agreeing with the ledger.
    assert excinfo.value.api_requests == len(served)
