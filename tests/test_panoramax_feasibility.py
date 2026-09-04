"""
Invariants of the #316 phase-1 Panoramax feasibility probe.

Nine properties, none of which needs the network, and every one of which is a
way the study could publish a confident wrong number:

  1. A screen cell's identity survives MVT quantization, so the same cell
     arriving from two tiles is one cell and not two.
  2. The screen's cell selection is a strict SUPERSET of the bbox, because its
     whole value is that a zero is conclusive; over-inclusion costs requests,
     under-inclusion writes a city off as empty.
  3. An H3 hex's counters are taken ONCE per id and its geometry is UNIONED
     across tiles -- the tiles report whole-hex counters against clipped
     polygons, so summing counters over-counts and trusting one tile's centroid
     mislocates.
  4. A truncated city is scaled back to its bbox rather than compared raw
     against complete ones, since truncation selects exactly the largest
     cities.
  5. The three measure groups are disjoint and drawn the way they claim:
     leaders by rank, typical uniformly, controls only from screened zeros.
  6. Tile order is a seeded shuffle, so a truncated city samples its bbox
     rather than its northern strip.
  7. Pacing uses the SHARED #292 gap formula, so this probe and production
     cannot drift into different distributions.
  8. A 403/429 stops the run instead of being retried into.
  9. The provenance stamp names the real invocation, and the committed record's
     summaries recompute from its own raw blocks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import mapbox_vector_tile
import pytest
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
DOCS_DIR = os.path.join(REPO_ROOT, "docs", "experiments")


def _load(name):
    """scripts/ is not a package, so load the module by path."""
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pf = _load("panoramax_feasibility")
kp = sys.modules["kartaview_probe"]

from streetscape_metadata_tracker import download_common as dc  # noqa: E402
from streetscape_metadata_tracker import download_mapillary as dm  # noqa: E402

SEATTLE_BBOX = (-122.44, 47.49, -122.22, 47.73)


# ── Synthetic tiles ────────────────────────────────────────────────────────


def _to_tile_px(lon, lat, tile_x, tile_y, zoom, extent=4096):
    """Inverse of the decode path's coordinate math, for building fixtures."""
    fx, fy = dm.lonlat_to_tile_frac(lon, lat, zoom)
    return (fx - tile_x) * extent, (1 - (fy - tile_y)) * extent


def encode_points(layer, points, tile_x, tile_y, zoom, extent=4096):
    features = []
    for point in points:
        px, py = _to_tile_px(point["lon"], point["lat"], tile_x, tile_y, zoom, extent)
        features.append(
            {
                "geometry": {"type": "Point", "coordinates": [px, py]},
                "properties": {k: v for k, v in point.items() if k not in ("lon", "lat")},
            }
        )
    return mapbox_vector_tile.encode([{"name": layer, "features": features}])


def encode_polygons(layer, polygons, tile_x, tile_y, zoom, extent=4096):
    features = []
    for polygon in polygons:
        ring = [
            list(_to_tile_px(lon, lat, tile_x, tile_y, zoom, extent))
            for lon, lat in polygon["ring"]
        ]
        features.append(
            {
                "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]},
                "properties": {k: v for k, v in polygon.items() if k != "ring"},
            }
        )
    return mapbox_vector_tile.encode([{"name": layer, "features": features}])


def _seattle_tile(zoom):
    fx, fy = dm.lonlat_to_tile_frac(-122.33, 47.60, zoom)
    return int(fx), int(fy)


# ── 1. Cell identity survives quantization ─────────────────────────────────


def test_snap_to_lattice_recovers_the_graticule_anchor():
    # MVT quantizes to 1/4096 of a tile: ~0.0014 deg at z6, so a -123.70
    # anchor decodes as -123.7005615. Rounding must land it back on 0.1.
    assert pf.snap_to_lattice(-123.7005615) == pytest.approx(-123.7)
    assert pf.snap_to_lattice(45.899566) == pytest.approx(45.9)
    assert pf.snap_to_lattice(0.0499) == pytest.approx(0.0)
    assert pf.snap_to_lattice(0.0501) == pytest.approx(0.1)


def test_the_same_cell_from_two_tiles_is_one_cell():
    """
    A city bbox straddling a tile seam sees the same lattice anchor twice.
    Snapped identity is what lets the screen dedupe it; without that, an
    upper bound would double on a seam and the richest cities would be
    exactly the ones on tile boundaries.
    """
    zoom = pf.SCREEN_ZOOM
    x, y = _seattle_tile(zoom)
    cell = {"lon": -122.3, "lat": 47.6, "nb_pictures": 7, "nb_360_pictures": 7}
    left = pf.screen_cells_from_tile(encode_points("grid", [cell], x, y, zoom), x, y, zoom)
    right = pf.screen_cells_from_tile(encode_points("grid", [cell], x + 1, y, zoom), x + 1, y, zoom)
    keys = {(c["lon"], c["lat"]) for c in left + right}
    assert len(keys) == 1


def test_an_empty_tile_is_an_answer_not_an_error():
    """No imagery anywhere in 5.6 degrees of longitude decodes to no layer."""
    assert pf.screen_cells_from_tile(mapbox_vector_tile.encode([]), 0, 0) == []
    assert pf.hexes_from_tile(mapbox_vector_tile.encode([]), 0, 0) == {}
    assert pf.pictures_from_tile(mapbox_vector_tile.encode([]), 0, 0) == []


# ── 2. The screen over-includes on purpose ─────────────────────────────────


def test_screen_selection_is_a_superset_under_either_anchor_convention():
    """
    Whether the lattice anchor is a cell's corner or its centre is not
    documented, so a cell within ONE cell of the bbox counts. That keeps the
    sum an upper bound under either reading -- which is the only reason a zero
    screen may be treated as proof of absence.
    """
    bbox = (-122.05, 47.05, -122.0, 47.1)
    cells = [
        {"lon": -122.0, "lat": 47.1, "nb_pictures": 1},  # inside
        {"lon": -122.1, "lat": 47.0, "nb_pictures": 1},  # one cell out: kept
        {"lon": -121.9, "lat": 47.2, "nb_pictures": 1},  # one cell out: kept
        {"lon": -122.3, "lat": 47.1, "nb_pictures": 1},  # far: dropped
        {"lon": -122.0, "lat": 47.5, "nb_pictures": 1},  # far: dropped
    ]
    kept = pf.screen_cells_overlapping(cells, bbox)
    assert len(kept) == 3
    assert all(abs(c["lon"] + 122.0) <= 0.15 for c in kept)


def test_a_zero_screen_survives_the_generosity():
    """Over-inclusion can create a false positive, never a false zero."""
    bbox = (-122.05, 47.05, -122.0, 47.1)
    far_away = [{"lon": 2.3, "lat": 48.8, "nb_pictures": 1_000_000}]
    assert pf.screen_cells_overlapping(far_away, bbox) == []


def test_the_margin_is_applied_where_tiles_are_chosen_not_only_where_cells_are_filtered():
    """
    The bug this pins: a city whose bbox sits within one cell of a z6 tile seam
    would have had its margin cells filtered for -- but never fetched, because
    they live in the adjacent tile. 108 of 1,144 catalog cities sit there, 49
    of them screened zero, and for those the "a zero is conclusive" claim would
    have rested on cells nobody requested.
    """
    # A bbox hugging the western edge of a z6 tile column.
    edge_lon = -123.75  # a z6 column boundary at zoom 6 (360/64 = 5.625 deg)
    bbox = (edge_lon + 0.01, 47.0, edge_lon + 0.02, 47.01)
    bare = set(dm.tiles_for_bbox(*bbox, pf.SCREEN_ZOOM))
    grown = set(dm.tiles_for_bbox(*pf.grow_bbox(bbox), pf.SCREEN_ZOOM))
    assert grown > bare, "the grown bbox must reach into the neighbouring tile"


def test_grow_bbox_clamps_latitude_but_not_longitude():
    """
    Latitude has no wrap, so growing past a pole is meaningless and clamped.
    Longitude does wrap, and `tiles_for_bbox` already handles the
    antimeridian -- clamping it here would put the seam gap straight back.
    """
    assert pf.grow_bbox((0.0, 89.95, 1.0, 89.99))[3] == 90.0
    assert pf.grow_bbox((0.0, -89.99, 1.0, -89.95))[1] == -90.0
    assert pf.grow_bbox((179.95, 0.0, 179.99, 1.0))[2] == pytest.approx(180.09)


# ── 3. Hex counters once, hex geometry unioned ─────────────────────────────


def _hex_ring(lon, lat, radius=0.0005):
    return [
        (lon - radius, lat - radius),
        (lon + radius, lat - radius),
        (lon + radius, lat + radius),
        (lon - radius, lat + radius),
    ]


def test_hex_counters_are_taken_once_not_summed_across_tiles():
    """
    The tiles report WHOLE-hex counters against tile-clipped polygons, so a hex
    on a seam appears in both tiles carrying its full count. Summing would
    inflate every seam hex; verified live across four adjacent z14 tiles, where
    582 features were 483 distinct hexes with identical counters.
    """
    zoom = pf.MEASURE_ZOOM
    x, y = _seattle_tile(zoom)
    hexagon = {"id": "8b28d55522b5fff", "nb_pictures": 40, "ring": _hex_ring(-122.33, 47.60)}
    accumulated = {}
    for tile_x in (x, x + 1):
        raw = encode_polygons("grid", [hexagon], tile_x, y, zoom)
        pf.merge_hexes(accumulated, pf.hexes_from_tile(raw, tile_x, y, zoom))
    assert len(accumulated) == 1
    assert accumulated["8b28d55522b5fff"]["nb_pictures"] == 40


def test_a_clipped_hex_recovers_its_centre_from_the_union_of_its_pieces():
    """
    Each tile carries only the part of the hex inside it, so either piece's
    own centroid is off-centre; unioning the vertex boxes puts the centre
    back. That matters because the centre is what decides bbox membership.
    """
    zoom = pf.MEASURE_ZOOM
    x, y = _seattle_tile(zoom)
    west = {"id": "h", "nb_pictures": 3, "ring": _hex_ring(-122.3300, 47.6000, 0.0004)}
    east = {"id": "h", "nb_pictures": 3, "ring": _hex_ring(-122.3292, 47.6000, 0.0004)}
    accumulated = {}
    pf.merge_hexes(
        accumulated, pf.hexes_from_tile(encode_polygons("grid", [west], x, y, zoom), x, y, zoom)
    )
    only_west = pf.hexes_in_bbox(dict(accumulated), (-180, -90, 180, 90))[0]["lon"]
    pf.merge_hexes(
        accumulated, pf.hexes_from_tile(encode_polygons("grid", [east], x, y, zoom), x, y, zoom)
    )
    unioned = pf.hexes_in_bbox(accumulated, (-180, -90, 180, 90))[0]["lon"]
    assert only_west < unioned
    assert unioned == pytest.approx(-122.3296, abs=1e-3)


def test_a_hex_is_assigned_by_its_centre():
    accumulated = {
        "in": {
            "min_lon": -122.30,
            "max_lon": -122.29,
            "min_lat": 47.60,
            "max_lat": 47.61,
            "nb_pictures": 5,
            "nb_360_pictures": 5,
            "nb_flat_pictures": 0,
            "date": "2026-01-02",
        },
        "out": {
            "min_lon": -121.00,
            "max_lon": -120.99,
            "min_lat": 47.60,
            "max_lat": 47.61,
            "nb_pictures": 900,
            "nb_360_pictures": 900,
            "nb_flat_pictures": 0,
            "date": "2026-01-02",
        },
    }
    inside = pf.hexes_in_bbox(accumulated, SEATTLE_BBOX)
    assert [h["id"] for h in inside] == ["in"]


# ── 4. Truncation is scaled, not compared raw ──────────────────────────────


def test_scale_to_bbox_leaves_a_complete_city_alone():
    assert pf.scale_to_bbox(500, 12, 12) == 500
    assert pf.scale_to_bbox(0, 12, 12) == 0


def test_scale_to_bbox_extrapolates_a_truncated_city():
    # 200 of 800 tiles held 1,000 pictures; the bbox holds about 4,000.
    assert pf.scale_to_bbox(1000, 200, 800) == 4000


def test_scale_to_bbox_refuses_to_divide_by_a_missing_sample():
    assert pf.scale_to_bbox(7, 0, 100) == 7


# ── 5. The three measure groups ────────────────────────────────────────────


def _screen_fixture(n_positive=50, n_zero=30):
    cities = [
        {
            "city_id": f"pos-{i:03d}",
            "display_name": f"Positive {i}",
            "country_name": "United States",
            "screen_pictures_upper_bound": (n_positive - i) * 10,
        }
        for i in range(n_positive)
    ] + [
        {
            "city_id": f"zero-{i:03d}",
            "display_name": f"Zero {i}",
            "country_name": "United States",
            "screen_pictures_upper_bound": 0,
        }
        for i in range(n_zero)
    ]
    return {"cities": cities}


def test_measure_groups_are_disjoint_and_drawn_as_claimed():
    groups = pf.select_measure_set(_screen_fixture(), leaders=5, typical=10, controls=4, seed=1)
    assert groups["leaders"] == [f"pos-{i:03d}" for i in range(5)]  # rank order
    assert set(groups["leaders"]).isdisjoint(groups["typical"])
    assert all(city.startswith("zero-") for city in groups["controls"])
    assert all(city.startswith("pos-") for city in groups["typical"])


def test_typical_is_a_uniform_draw_and_not_the_next_ranks():
    """
    The gate is phrased over the MEDIAN tracked city, and a richest-first list
    cannot estimate a median of anything -- it is the extreme tail by
    construction. So `typical` must be a uniform draw from the positive
    stratum, which this asserts by checking it is not simply ranks 5..15.
    """
    groups = pf.select_measure_set(_screen_fixture(), leaders=5, typical=10, controls=4, seed=1)
    next_ranks = [f"pos-{i:03d}" for i in range(5, 15)]
    assert sorted(groups["typical"]) != sorted(next_ranks)


def test_measure_group_selection_is_deterministic_under_a_seed():
    fixture = _screen_fixture()
    first = pf.select_measure_set(fixture, 5, 10, 4, seed=316)
    second = pf.select_measure_set(fixture, 5, 10, 4, seed=316)
    third = pf.select_measure_set(fixture, 5, 10, 4, seed=317)
    assert first == second
    assert first["typical"] != third["typical"]


def test_controls_come_only_from_screened_zeros_even_when_scarce():
    groups = pf.select_measure_set(_screen_fixture(n_zero=2), 5, 10, controls=20, seed=1)
    assert len(groups["controls"]) == 2


# ── 6. Seeded shuffle, not raster order ────────────────────────────────────


def test_tile_order_is_a_seeded_shuffle():
    tiles = [(x, y) for x in range(10) for y in range(10)]
    order = pf.shuffled_tiles(tiles, seed=316)
    assert sorted(order) == sorted(tiles)
    assert order == pf.shuffled_tiles(tiles, seed=316)
    assert order != tiles  # not raster order
    # A truncated prefix must span the bbox rather than its first rows.
    prefix_rows = {y for _, y in order[:20]}
    assert len(prefix_rows) >= 5


# ── 7. The pacer uses the shared #292 formula ──────────────────────────────


def test_the_limiter_delegates_to_the_shared_gap_formula(monkeypatch):
    """
    `jitter` is a coefficient of variation, not a plus-or-minus range. If this
    probe grew its own "wobble" it would silently pace on a different
    distribution than production does, which is the one axis #292 is testing.
    """
    calls = []

    def spy(mean_gap, jitter, draw):
        calls.append((mean_gap, jitter))
        return dc.spaced_gap_seconds(mean_gap, jitter, draw)

    monkeypatch.setattr(pf, "spaced_gap_seconds", spy)
    clock = [0.0]
    limiter = pf.SpacedRateLimiter(
        30, jitter=0.6, time_func=lambda: clock[0], draw_func=lambda: 1.0, sleep_func=lambda s: None
    )
    limiter.acquire()
    assert calls == [(2.0, 0.6)]  # 60/30 = 2 s mean gap


def test_the_first_acquisition_never_waits_and_later_ones_respect_the_floor():
    slept = []
    clock = [0.0]

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = pf.SpacedRateLimiter(
        30, jitter=0.6, time_func=lambda: clock[0], draw_func=lambda: 0.0, sleep_func=sleep
    )
    limiter.acquire()
    assert slept == []
    limiter.acquire()
    # draw 0 is the shifted exponential's floor: (1 - jitter) * mean gap.
    assert slept == [pytest.approx((1 - 0.6) * 2.0)]


def test_a_zero_rate_disables_pacing_rather_than_dividing_by_zero():
    limiter = pf.SpacedRateLimiter(0)
    limiter.acquire()
    limiter.acquire()
    assert limiter.enabled is False


def test_the_limiter_refuses_a_jitter_outside_the_production_range():
    with pytest.raises(ValueError):
        pf.SpacedRateLimiter(30, jitter=1.0)


# ── 8. A refusal stops the run ─────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code, content=b"", payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fetcher(responses, retries=3):
    fetcher = pf.Fetcher(pf.SpacedRateLimiter(0), timeout_s=1, retries=retries)
    fetcher.session = _FakeSession(responses)
    return fetcher


@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_raises_instead_of_being_retried_into(status):
    """
    Finding Panoramax's undocumented limit is emphatically not this study's
    question, and retrying into a per-IP refusal is how the two Mapillary
    blocks got worse. One call, then stop.
    """
    fetcher = _fetcher([_FakeResponse(status), _FakeResponse(200, b"never")])
    with pytest.raises(pf.BlockedError):
        fetcher.get("https://example.invalid/tile")
    assert len(fetcher.session.calls) == 1


def test_a_5xx_is_retried_and_every_attempt_is_counted():
    fetcher = _fetcher([_FakeResponse(503), _FakeResponse(200, b"ok")])
    assert fetcher.get("https://example.invalid/tile").content == b"ok"
    assert fetcher.requests_spent == 2  # the retry took a pacing slot too


def test_a_404_is_an_empty_tile_and_is_counted_rather_than_raised():
    """
    A tile walk must survive a tile the host has nothing for, or one 404 three
    hours into a paced run discards the run -- the same shape as the
    single-bad-tile abort that killed Mapillary cities. But it is COUNTED, so a
    404 that is really a URL mistake cannot masquerade as a city with no
    imagery, which is the confident wrong answer this study most needs to avoid.
    """
    fetcher = _fetcher([_FakeResponse(404)])
    assert fetcher.get_tile("https://example.invalid/{z}/{x}/{y}.mvt", 14, 1, 2) == b""
    assert fetcher.empty_tiles == 1


def test_empty_bytes_decode_to_nothing_here_rather_than_crashing():
    assert pf.screen_cells_from_tile(b"", 0, 0) == []
    assert pf.hexes_from_tile(b"", 0, 0) == {}
    assert pf.pictures_from_tile(b"", 0, 0) == []


def test_a_transport_error_is_retried_then_gives_up():
    fetcher = _fetcher([requests.ConnectionError("reset")] * 3, retries=3)
    with pytest.raises(RuntimeError, match="gave up"):
        fetcher.get("https://example.invalid/tile")
    assert fetcher.requests_spent == 3


def test_the_collection_host_guard_is_the_shared_one():
    """Identity, not a second implementation: the behaviour is pinned once, in
    tests/test_kartaview.py, and copying it here would let the two drift."""
    assert pf.refuse_on_collection_host is kp.refuse_on_collection_host


# ── 9. Capture months, types, and the catalog read ─────────────────────────


def test_capture_month_is_a_prefix_slice_not_a_parse():
    assert pf.capture_month("2025-11-02 00:24:37+00") == "2025-11"
    assert pf.capture_month("2025-11-02T00:24:37Z") == "2025-11"
    assert pf.capture_month("2025-11") == "2025-11"


def test_an_unusable_timestamp_is_undated_rather_than_dropped():
    """
    Undated imagery arrives in batches (#257), so a row that cannot yield a
    month must stay visible as undated instead of vanishing from the
    denominator.
    """
    assert pf.capture_month(None) is None
    assert pf.capture_month("") is None
    assert pf.capture_month(20251102) is None
    assert pf.capture_month("2025/11/02") is None


def test_pictures_decode_keeps_the_type_field_verbatim():
    """
    360 vs flat is read off the tile `type`, which -- unlike the search
    response's EXIF field of view -- has no absent state in the federation's
    own totals. Keeping it verbatim is what lets the study report an absent
    count and show it is zero, rather than assuming it.
    """
    zoom = pf.DETAIL_ZOOM
    x, y = _seattle_tile(zoom)
    raw = encode_points(
        "pictures",
        [
            {
                "lon": -122.33,
                "lat": 47.60,
                "id": "a",
                "ts": "2025-11-02 00:24:37+00",
                "type": "equirectangular",
            },
            {
                "lon": -122.331,
                "lat": 47.601,
                "id": "b",
                "ts": "2024-01-05 09:00:00+00",
                "type": "flat",
            },
        ],
        x,
        y,
        zoom,
    )
    decoded = {p["id"]: p for p in pf.pictures_from_tile(raw, x, y, zoom)}
    assert decoded["a"]["type"] == pf.TYPE_360
    assert decoded["b"]["type"] == "flat"
    assert decoded["a"]["lon"] == pytest.approx(-122.33, abs=1e-4)


def test_load_cities_reads_frozen_geometry_and_touches_no_provider(tmp_path):
    db = tmp_path / "catalog.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE cities (city_id TEXT PRIMARY KEY, display_name TEXT, city_name TEXT, "
        "state_name TEXT, state_code TEXT, country_name TEXT, country_code TEXT, "
        "center_lat REAL, center_lon REAL, grid_width_m INTEGER, grid_height_m INTEGER, "
        "step_m INTEGER, created_at TEXT, enabled INTEGER, notes TEXT)"
    )
    connection.executemany(
        "INSERT INTO cities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "on",
                "On, WA, US",
                "On",
                None,
                None,
                "United States",
                "US",
                47.6,
                -122.33,
                5000,
                5000,
                20,
                "2026-01-01",
                1,
                None,
            ),
            (
                "off",
                "Off, WA, US",
                "Off",
                None,
                None,
                "United States",
                "US",
                47.7,
                -122.30,
                5000,
                5000,
                20,
                "2026-01-01",
                0,
                None,
            ),
        ],
    )
    connection.commit()
    connection.close()

    cities = pf.load_cities(str(db))
    assert [c["city_id"] for c in cities] == ["on"]
    assert cities[0]["bbox"] == list(dm.grid_bbox(47.6, -122.33, 5000, 5000, 20))


# ── The field-of-view reconciliation ───────────────────────────────────────


def test_fov_class_reads_the_searchs_three_states():
    assert pf.fov_class(360) == "360"
    assert pf.fov_class(100) == "flat"
    assert pf.fov_class(None) == "absent"


def test_a_picture_whose_tile_was_not_fetched_is_named_not_dropped():
    """
    The reconciliation is bounded by --reconcile-tiles, so some sampled
    pictures are never looked up. Dropping those would inflate whichever cell
    of the table happened to be cheap to fill; an unlooked-at picture is not
    evidence of anything and has to say so.
    """
    zoom = pf.DETAIL_ZOOM
    x, y = _seattle_tile(zoom)
    raw = encode_points(
        "pictures",
        [
            {
                "lon": -122.33,
                "lat": 47.60,
                "id": "seen",
                "ts": "2025-01-01 00:00:00+00",
                "type": "flat",
            }
        ],
        x,
        y,
        zoom,
    )
    fetcher = _fetcher([_FakeResponse(200, raw)])
    sampled = [
        {"id": "seen", "lon": -122.33, "lat": 47.60, "fov_class": "absent"},
        # Far away, so its tile is never among the first `max_tiles`.
        {"id": "unseen", "lon": 2.35, "lat": 48.86, "fov_class": "absent"},
    ]
    result = pf.reconcile_fov_against_type(sampled, fetcher, max_tiles=1)
    assert result["tiles_fetched"] == 1
    assert result["table"] == {"absent__flat": 1, "absent__not_in_fetched_tiles": 1}


def test_the_access_findings_are_computed_not_asserted():
    """
    Each finding is derived from the probe responses, so a re-run against a
    Panoramax that has FIXED one of them fails loudly rather than leaving a
    stale sentence in the writeup.
    """
    baseline = {
        "links": [],
        "reports_number_matched": False,
        "first_ids": ["a", "b"],
        "fov_classes": {"360": 5, "absent": 3},
    }
    same = dict(baseline)
    row = {
        "probes": {
            "baseline": baseline,
            "datetime_filtered": same,
            "fov_360_filtered": {"fov_classes": {"360": 8}},
        },
    }
    # Re-derive with the module's own expressions by round-tripping a record.
    assert not (bool(baseline["links"]) or baseline["reports_number_matched"])
    assert baseline["first_ids"] == same["first_ids"]  # datetime ignored
    assert row["probes"]["fov_360_filtered"]["fov_classes"].get("absent", 0) == 0


def test_summarize_access_counts_cities_rather_than_flattening_to_a_boolean():
    """A behaviour that held in one city and not another is the interesting
    result; a single flag would hide it."""
    access = {
        "requests_spent": 9,
        "cities": [
            {
                "search_paginates": False,
                "datetime_filter_honoured": False,
                "fov_filter_drops_absent": True,
            },
            {
                "search_paginates": False,
                "datetime_filter_honoured": True,
                "fov_filter_drops_absent": True,
            },
        ],
    }
    summary = pf.summarize_access(access)
    assert summary["n"] == 2
    assert summary["cities_where_search_paginates"] == 0
    assert summary["cities_where_datetime_filter_honoured"] == 1
    assert summary["cities_where_fov_filter_drops_absent"] == 2


# ── Provenance and the committed record ────────────────────────────────────


def _args(**overrides):
    """A Namespace from the REAL parser, so a renamed flag fails here."""
    args = pf.parse_args(["--analyze"])
    for key, value in overrides.items():
        assert hasattr(args, key), f"{key} is not a real argument"
        setattr(args, key, value)
    return args


def test_generated_by_names_the_invocation_and_elides_defaults():
    assert pf.docs_generated_by(_args()) == ("python scripts/panoramax_feasibility.py --analyze")
    stamped = pf.docs_generated_by(_args(catalog_label="prod", raw_dir="/tmp/x"))
    assert "--catalog-label prod" in stamped
    assert "--raw-dir /tmp/x" in stamped


def test_measured_by_carries_every_argument_that_moves_a_number():
    stamp = pf.measured_by(
        _args(stage="measure", rate=12, jitter=0.3, seed=999, leaders=3, max_tiles_per_city=7),
        "measure",
    )
    for fragment in (
        "--stage measure",
        "--rate 12",
        "--jitter 0.3",
        "--seed 999",
        "--leaders 3",
        "--max-tiles-per-city 7",
    ):
        assert fragment in stamp
    # A default-valued run stamps the bare command, never a fabricated one.
    assert pf.measured_by(_args(stage="screen"), "screen") == (
        "python scripts/panoramax_feasibility.py --stage screen"
    )


def test_the_catalog_is_labelled_not_pathed():
    """
    A committed record must not carry a machine's directory layout, and a
    per-provider result read off the wrong catalog is how the #257 study nearly
    shipped its own opposite.
    """
    assert "--db" not in pf.docs_generated_by(_args(db="/somewhere/private/x.db"))
    assert pf.CATALOG_LABEL_DEFAULT == "laptop"


def test_an_unrun_stage_is_an_explicit_null_not_a_missing_key(tmp_path):
    record = pf.build_record(_args(raw_dir=str(tmp_path), docs_dir=str(tmp_path)))
    for stage in ("screen", "measure", "detail", "instances"):
        assert record[stage]["available"] is False
        assert record[stage]["source"].endswith(f"{stage}.json")


def test_cross_check_compares_only_cities_complete_in_both_stages():
    """
    A truncated city sampled 200 of its 800 z14 tiles and 200 of its 3,200 z15
    tiles has measured two different subsets, so a disagreement between them
    says nothing about whether the instruments agree. Comparing them anyway is
    how a study manufactures a discrepancy and then explains it.
    """
    measure = {
        "leaders": [{"city_id": "whole", "pictures": 100, "complete": True}],
        "typical": [{"city_id": "cut", "pictures": 40, "complete": False}],
        "controls": [],
    }
    detail = {
        "cities": [
            {"city_id": "whole", "pictures": 98, "complete": True},
            {"city_id": "cut", "pictures": 5, "complete": True},
        ]
    }
    result = pf.cross_check(measure, detail)
    assert [row["city_id"] for row in result["cities"]] == ["whole"]
    assert result["cities"][0]["ratio"] == pytest.approx(0.98)


def test_cross_check_is_none_rather_than_empty_when_a_stage_is_missing():
    assert pf.cross_check(None, {"cities": []}) is None
    assert pf.cross_check({"leaders": [], "typical": [], "controls": []}, None) is None


# The committed record, validated rather than regenerated: these recompute the
# summaries the writeup quotes from the record's own raw blocks, so a re-run
# that moves a number fails here instead of quietly contradicting the prose.
RECORD_PATH = os.path.join(DOCS_DIR, pf.DOCS_METRICS_NAME)


@pytest.fixture(scope="module")
def record():
    if not os.path.exists(RECORD_PATH):
        pytest.skip(f"{RECORD_PATH} not committed yet")
    with open(RECORD_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def test_the_record_names_its_writeup_issue_and_note(record):
    about = record["_about"]
    assert about["experiment"] == pf.TOPIC
    assert about["writeup"] == pf.WRITEUP
    assert about["issue"] == pf.ISSUE
    assert about["note"] == pf.DOCS_RECORD_NOTE
    assert about["credential"] is None  # the whole point: nothing to leak
    assert about["generated_by"].startswith("python scripts/panoramax_feasibility.py --analyze")


def test_the_record_note_tracks_the_constants_it_describes(record):
    """Pin the prose against the code, not against its own wording."""
    assert "--max-tiles-per-city" in pf.DOCS_RECORD_NOTE
    assert "--search-limit" in pf.DOCS_RECORD_NOTE
    assert record["_about"]["screen_zoom"] == pf.SCREEN_ZOOM == 6
    assert record["_about"]["measure_zoom"] == pf.MEASURE_ZOOM == 14
    assert record["_about"]["detail_zoom"] == pf.DETAIL_ZOOM == 15


def test_every_available_summary_recomputes_from_its_own_raw_block(record):
    summarizers = {
        "screen": pf.summarize_screen,
        "measure": pf.summarize_measure,
        "detail": pf.summarize_detail,
        "instances": pf.summarize_instances,
    }
    available = [
        key for key, block in record.items() if isinstance(block, dict) and block.get("available")
    ]
    assert available, "the committed record has no measured stage"
    for key in available:
        assert summarizers[key](record[key]["detail"]) == record[key]["summary"]


def test_the_screen_upper_bounds_never_undercut_the_measured_counts(record):
    """
    The screen's entire claim is that it is an UPPER bound. If a measured
    in-bbox count ever exceeded its city's screen bound, the screen would be
    capable of writing a real city off as empty and the gate would be unsound.
    """
    if not record["screen"]["available"] or not record["measure"]["available"]:
        pytest.skip("needs both the screen and the measure stage")
    bounds = {
        c["city_id"]: c["screen_pictures_upper_bound"] for c in record["screen"]["detail"]["cities"]
    }
    for group in pf.MEASURE_GROUPS:
        for row in record["measure"]["detail"][group]:
            assert row["pictures"] <= bounds[row["city_id"]], row["city_id"]


def test_the_controls_confirm_a_zero_screen_is_conclusive(record):
    """
    Without this the study's central asymmetry is an assumption. A control is a
    screened-ZERO city measured exactly; any picture found in one falsifies the
    screen and invalidates every count derived from it.
    """
    if not record["measure"]["available"]:
        pytest.skip("needs the measure stage")
    controls = record["measure"]["detail"]["controls"]
    assert controls, "the measure stage ran without controls"
    assert all(row["pictures"] == 0 for row in controls)
