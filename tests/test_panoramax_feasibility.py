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


def test_seam_hex_counters_take_the_max_across_sightings_never_the_sum_or_the_first():
    """
    Under the measured contract every sighting carries the same whole-hex
    figure, so max is exact. If a tile ever carried only its own piece's
    count, first-seen could record a ZERO for a hex whose pictures all sit in
    the other tile -- and the screen would call a covered city empty. Max
    cannot: it is zero only when every piece is zero.
    """
    zoom = pf.MEASURE_ZOOM
    x, y = _seattle_tile(zoom)
    empty_piece = {"id": "h", "nb_pictures": 0, "nb_360_pictures": 0, "nb_flat_pictures": 0}
    full_piece = {"id": "h", "nb_pictures": 40, "nb_360_pictures": 30, "nb_flat_pictures": 10}
    accumulated = {}
    for tile_x, piece in ((x, empty_piece), (x + 1, full_piece)):
        raw = encode_polygons(
            "grid", [{**piece, "ring": _hex_ring(-122.33, 47.60)}], tile_x, y, zoom
        )
        pf.merge_hexes(accumulated, pf.hexes_from_tile(raw, tile_x, y, zoom))
    assert accumulated["h"]["nb_pictures"] == 40  # not 0 (first), not 40 + 0 either way
    assert (accumulated["h"]["nb_360_pictures"], accumulated["h"]["nb_flat_pictures"]) == (30, 10)


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


# ── The two z6 grids disagree, and reuse is guarded ────────────────────────


def test_the_screen_selects_hexes_by_OVERLAP_not_by_centre():
    """
    The screen and the measure stage select hexes differently on purpose. A
    res-11 hexagon is 25 m across so its centre is as good as its extent; a
    res-6 SCREEN hexagon is ~36 km2 and a city bbox is often smaller than one,
    so centre-based selection would miss the very hex the city sits inside --
    turning a covered city into a screened zero, which is the one failure the
    design cannot tolerate.
    """
    # A hex far larger than the bbox, whose centre lies outside it.
    accumulated = {
        "big": {
            "min_lon": -123.0,
            "max_lon": -122.0,
            "min_lat": 47.0,
            "max_lat": 48.0,
            "nb_pictures": 9,
            "nb_360_pictures": 9,
            "nb_flat_pictures": 0,
            "date": None,
        }
    }
    tiny = (-122.05, 47.90, -122.02, 47.93)
    assert pf.hexes_in_bbox(accumulated, tiny) == []  # centre: misses it
    assert len(pf.hexes_overlapping_bbox(accumulated, tiny)) == 1  # overlap: catches it


def test_the_default_screen_variant_is_the_one_that_is_not_lossy():
    """
    The v1 lattice is what the API root's `xyz` link points at, and it drops
    populated cells: it reported 2.5%, 7.9% and 23.9% fewer pictures than the
    v2 H3 grid over three identical z6 extents, and a control city it called
    empty held imagery. A lossy screen is not a slightly worse screen here --
    the whole design rests on a zero being conclusive.
    """
    assert pf.DEFAULT_SCREEN_VARIANT == "v2_h3"
    assert set(pf.SCREEN_VARIANTS) == {"v1_lattice", "v2_h3"}


def _prior(city_id="c", tiles_total=49, tiles_probed=49, seed=316, record_seed=True):
    row = {
        "city_id": city_id,
        "tiles_total": tiles_total,
        "tiles_probed": tiles_probed,
        "pictures": 7,
    }
    if record_seed:
        row["seed"] = seed
    return {
        "_measured_by": "python scripts/panoramax_feasibility.py --stage measure",
        "leaders": [row],
        "typical": [],
        "controls": [],
    }


def _city(city_id="c"):
    return {
        "city_id": city_id,
        "display_name": "C",
        "country_name": "US",
        "bbox": list(dm.grid_bbox(47.6, -122.33, 5000, 5000, 20)),
    }


def test_a_prior_row_is_reused_only_when_the_tile_plan_matches():
    city = _city()
    n = len(dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM))
    good = pf.reusable_measure_row(_prior(tiles_total=n, tiles_probed=n), city, 200, 316)
    assert good is not None and good["pictures"] == 7
    assert good["reused_from"].startswith("python scripts/panoramax_feasibility.py")


def test_a_prior_row_measured_under_different_settings_is_refetched():
    """
    The guard, not the determinism, is what makes reuse safe: a row truncated
    at a different --max-tiles-per-city sampled a different subset of the same
    bbox, and comparing it beside fresh rows would be comparing two things.
    """
    city = _city()
    n = len(dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM))
    assert (
        pf.reusable_measure_row(_prior(tiles_total=n, tiles_probed=n // 2), city, 200, 316) is None
    )
    assert (
        pf.reusable_measure_row(_prior(tiles_total=n + 1, tiles_probed=n + 1), city, 200, 316)
        is None
    )
    assert pf.reusable_measure_row(None, city, 200, 316) is None
    assert pf.reusable_measure_row(_prior(city_id="other"), city, 200, 316) is None


def test_a_complete_row_is_reused_across_seeds_because_its_plan_is_seed_independent():
    """
    The seed only selects WHICH tiles when the plan is truncated. A complete
    city visits every tile and `hexes_in_bbox` sorts by hex id, so its row is
    the same number under any seed and refetching it would spend requests
    against a host with no documented limit to re-learn a number we hold.
    """
    city = _city()
    n = len(dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM))
    prior = _prior(tiles_total=n, tiles_probed=n, seed=316)
    assert pf.reusable_measure_row(prior, city, 200, 316) is not None
    assert pf.reusable_measure_row(prior, city, 200, 999) is not None


def test_a_truncated_row_is_refetched_when_the_seed_changes():
    """
    For a truncated city the seed IS the sample: `shuffled_tiles(tiles, seed)`
    picks a different subset, so `pictures` and `pictures_scaled_to_bbox` both
    move while `tiles_total` and `tiles_probed` stay identical. Comparing only
    those two counts let a changed --seed through silently, and the run then
    stamped the NEW seed into `_measured_by` over rows drawn under the OLD one
    -- a false provenance claim in a committed record.
    """
    city = _city()
    n = len(dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM))
    assert n > 5, "the fixture city has to be big enough to truncate"
    prior = _prior(tiles_total=n, tiles_probed=5, seed=316)
    assert pf.reusable_measure_row(prior, city, 5, 316) is not None
    assert pf.reusable_measure_row(prior, city, 5, 317) is None
    # And the seeds really do choose different tiles, or the guard above would
    # be pinning a distinction that does not exist.
    tiles = dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM)
    assert pf.shuffled_tiles(tiles, 316)[:5] != pf.shuffled_tiles(tiles, 317)[:5]


def test_a_truncated_row_that_records_no_seed_is_refetched():
    """Unknown provenance is not matching provenance. Rows written before the
    field existed cannot be shown to match, so they are not assumed to."""
    city = _city()
    n = len(dm.tiles_for_bbox(*city["bbox"], pf.MEASURE_ZOOM))
    legacy_truncated = _prior(tiles_total=n, tiles_probed=5, record_seed=False)
    legacy_complete = _prior(tiles_total=n, tiles_probed=n, record_seed=False)
    assert pf.reusable_measure_row(legacy_truncated, city, 5, 316) is None
    # A complete legacy row is still exact, so it is still reusable.
    assert pf.reusable_measure_row(legacy_complete, city, 200, 316) is not None


def test_measure_city_records_the_seed_that_selected_its_tiles():
    """`reusable_measure_row` cannot honour its contract without this field."""
    city = _city()
    row = pf.measure_city(city, _TileFetcher({}), max_tiles=3, seed=4242)
    assert row["seed"] == 4242
    assert row["tiles_probed"] == 3 and row["complete"] is False


def test_the_measure_loop_refetches_a_truncated_row_when_the_seed_moves(tmp_path):
    """
    The guard through the loop rather than through the function, because that
    is where --reuse-measured is actually applied: same seed reuses and spends
    nothing, a moved seed refetches and spends the cap.
    """
    cities = [_city("a")]
    by_id = pf._cities_by_id(cities)
    n = len(dm.tiles_for_bbox(*cities[0]["bbox"], pf.MEASURE_ZOOM))
    pf.write_raw(str(tmp_path), "measure", _prior("a", tiles_total=n, tiles_probed=2, seed=316))

    same = _measure_args(tmp_path, "--city", "a", "--reuse-measured", "--max-tiles-per-city", "2")
    kept = pf._run_stage(same, cities, by_id, _TileFetcher({}))
    assert kept["reused_rows"] == 1 and kept["requests_spent"] == 0

    moved = _measure_args(
        tmp_path, "--city", "a", "--reuse-measured", "--max-tiles-per-city", "2", "--seed", "317"
    )
    fetcher = _TileFetcher({})
    fresh = pf._run_stage(moved, cities, by_id, fetcher)
    assert fresh["reused_rows"] == 0
    assert fresh["requests_spent"] == fetcher.requests_spent == 2
    assert fresh["leaders"][0]["seed"] == 317


def test_reuse_is_stamped_into_the_provenance():
    args = _args(stage="measure", reuse_measured=True)
    assert "--reuse-measured" in pf.measured_by(args, "measure")
    assert "--reuse-measured" not in pf.measured_by(_args(stage="measure"), "measure")


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


def _search_feature(picture_id, fov, stamp="2025-06-01T00:00:00Z", lon=-122.33, lat=47.60):
    return {
        "id": picture_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "datetime": stamp,
            "pers:interior_orientation": {"field_of_view": fov} if fov is not None else {},
        },
        "links": [{"rel": "via", "instance_name": "IGN", "href": "https://ign.invalid"}],
    }


def _search_response(features, links=(), number_matched=None):
    payload = {"features": features, "links": list(links)}
    if number_matched is not None:
        payload["numberMatched"] = number_matched
    return _FakeResponse(200, json.dumps(payload).encode(), payload)


# Two pictures that differ on BOTH axes the access probe reads: the newest
# capture (which is what the datetime window is derived from) and the EXIF
# field of view. One picture older than the other is what gives the datetime
# question something to drop.
NEWEST = "2026-08-01T00:00:00Z"
OLDER = "2025-06-01T00:00:00Z"
A360 = _search_feature("a", 360, stamp=NEWEST)
B_ABSENT = _search_feature("b", None, stamp=OLDER)
DERIVED_WINDOW = "2026-08-01T00:00:00Z/.."


def test_the_datetime_window_is_derived_from_the_citys_own_newest_capture():
    """
    The window is a per-city derivation, not a constant. It was a constant --
    `2026-01-01T00:00:00Z/..`, justified as "deliberately in the FUTURE
    relative to the imagery it is aimed at" -- and it silently stopped being
    in the future eight months before the study ran.
    """
    window, cutoff, droppable = pf.access_window([A360, B_ABSENT])
    assert window == DERIVED_WINDOW
    assert cutoff == pf.parse_stamp(NEWEST)
    assert droppable == 1  # exactly the one picture older than the cutoff


def test_the_window_is_derived_chronologically_not_lexicographically():
    """
    The endpoint mixes `Z` with `+00:00` and seconds with microseconds, and
    string order across those forms is not time order: `'2026-08-01T00:00:00Z'`
    sorts AFTER `'2026-09-01T00:00:00.5+00:00'` on no axis that matters, but a
    naive max over the raw strings gets pairs like these wrong.
    """
    early = _search_feature("early", 360, stamp="2026-08-01T00:00:00Z")
    late = _search_feature("late", 360, stamp="2026-09-01T00:00:00.500000+00:00")
    window, cutoff, droppable = pf.access_window([early, late])
    assert cutoff == pf.parse_stamp("2026-09-01T00:00:00.5+00:00")
    assert window.endswith("/..") and window.startswith("2026-09-01T00:00:00.500000Z")
    assert droppable == 1


def test_an_empty_baseline_falls_back_rather_than_inventing_a_window():
    """Nothing to derive from, and nothing the probe could distinguish."""
    window, cutoff, droppable = pf.access_window([])
    assert window == pf.ACCESS_PROBE_FALLBACK_DATETIME
    assert cutoff is None and droppable == 0


def test_an_unparseable_or_missing_capture_time_is_outside_every_window():
    """
    A picture the filter should have excluded but whose date cannot be read is
    evidence the filter did not exclude it -- the conservative direction, since
    the finding under test is that the filter does nothing.
    """
    cutoff = pf.parse_stamp(NEWEST)
    undated = {"id": "u", "properties": {}}
    unparseable = {"id": "x", "properties": {"datetime": "last tuesday"}}
    assert pf.features_before([undated, unparseable], cutoff) == 2
    assert pf.features_before([A360], cutoff) == 0
    assert pf.features_before([A360, B_ABSENT], cutoff) == 1
    assert pf.features_before([B_ABSENT], None) == 0  # no window, no claim


@pytest.mark.parametrize(
    "responses, expected",
    [
        (
            # What Panoramax did on 2026-09-04: no links, the same rows back
            # under a datetime filter, and the EXIF-less picture gone under
            # the field-of-view filter.
            [
                _search_response([A360, B_ABSENT]),
                _search_response([A360, B_ABSENT]),
                _search_response([A360]),
            ],
            {
                "search_paginates": False,
                "datetime_can_answer": True,
                "datetime_filter_honoured": False,
                "fov_can_answer": True,
                "fov_filter_drops_absent": True,
            },
        ),
        (
            # A Panoramax that fixed all three: every finding must flip, or
            # the writeup's three sentences would outlive the behaviour.
            [
                _search_response([A360, B_ABSENT], links=[{"rel": "next", "href": "..."}]),
                _search_response([A360]),
                _search_response([A360, B_ABSENT]),
            ],
            {
                "search_paginates": True,
                "datetime_can_answer": True,
                "datetime_filter_honoured": True,
                "fov_can_answer": True,
                "fov_filter_drops_absent": False,
            },
        ),
        (
            # A match count is pagination enough, even with no links.
            [
                _search_response([A360, B_ABSENT], number_matched=2),
                _search_response([A360, B_ABSENT]),
                _search_response([A360]),
            ],
            {
                "search_paginates": True,
                "datetime_can_answer": True,
                "datetime_filter_honoured": False,
                "fov_can_answer": True,
                "fov_filter_drops_absent": True,
            },
        ),
        (
            # A filter that drops SOME of what it should but not all is not
            # honoured. Reading the first five ids instead of the window would
            # have called this one honoured, since the first id moved.
            [
                _search_response([A360, B_ABSENT]),
                _search_response([B_ABSENT]),
                _search_response([A360]),
            ],
            {
                "search_paginates": False,
                "datetime_can_answer": True,
                "datetime_filter_honoured": False,
                "fov_can_answer": True,
                "fov_filter_drops_absent": True,
            },
        ),
    ],
)
def test_the_access_findings_are_derived_from_the_responses(responses, expected):
    """
    Each finding is computed from the probe responses by `stage_access`, so a
    re-run against a Panoramax that has FIXED one of them fails loudly rather
    than leaving a stale sentence in the writeup. (An earlier version of this
    test re-asserted the expressions on dict literals and never called the
    stage at all, which pinned nothing.)
    """
    fetcher = _fetcher(responses)
    row = pf.stage_access(_city(), fetcher)
    assert {key: row[key] for key in expected} == expected
    assert [params["limit"] for _, params in fetcher.session.calls] == [pf.ACCESS_PROBE_LIMIT] * 3
    assert fetcher.session.calls[1][1]["datetime"] == DERIVED_WINDOW == row["datetime_window"]
    assert fetcher.session.calls[2][1]["filter"] == "field_of_view=360"
    assert row["probes"]["baseline"]["fov_classes"] == {"360": 1, "absent": 1}


def test_a_city_whose_imagery_all_postdates_a_fixed_cutoff_can_still_answer():
    """
    The Boise regression, and the reason the window stopped being a constant.

    Boise's whole Panoramax deployment is three months old: its 300 baseline
    rows span 27 hours in 2026-08, every one of them AFTER the old fixed
    `2026-01-01T00:00:00Z` cutoff. An honoured filter over that window returns
    exactly the rows an ignored one returns, so the old probe read "ignored"
    whichever was true -- and Boise was counted among the twenty cities said
    to show the filter ignored, while being no evidence at all.

    Derived from the city's own newest capture the same city is decisive, and
    decisive in BOTH directions.
    """
    early = _search_feature("early", 360, stamp="2026-08-26T19:46:52Z")
    late = _search_feature("late", 360, stamp="2026-08-27T21:27:43Z")
    assert pf.parse_stamp("2026-08-26T19:46:52Z") > pf.parse_stamp(
        pf.ACCESS_PROBE_FALLBACK_DATETIME.split("/")[0]
    ), "the fixture must postdate the retired constant, or it pins nothing"

    ignored = pf.stage_access(
        _city(),
        _fetcher(
            [
                _search_response([early, late]),
                _search_response([early, late]),
                _search_response([early, late]),
            ]
        ),
    )
    assert ignored["datetime_can_answer"] is True
    assert ignored["datetime_filter_honoured"] is False

    honoured = pf.stage_access(
        _city(),
        _fetcher(
            [
                _search_response([early, late]),
                _search_response([late]),
                _search_response([early, late]),
            ]
        ),
    )
    assert honoured["datetime_can_answer"] is True
    assert honoured["datetime_filter_honoured"] is True


def test_a_city_with_one_capture_instant_cannot_answer_the_datetime_question():
    """
    Every picture at the cutoff means an honoured filter drops nothing, so
    `outside == 0` says nothing. That must be no evidence, never a
    confirmation -- the same rule the field-of-view probe already follows.
    """
    same = [_search_feature("a", 360, stamp=NEWEST), _search_feature("b", 360, stamp=NEWEST)]
    row = pf.stage_access(
        _city(),
        _fetcher([_search_response(same), _search_response(same), _search_response(same)]),
    )
    assert row["datetime_baseline_droppable"] == 0
    assert row["datetime_can_answer"] is False
    assert row["datetime_filter_honoured"] is False  # not a yes despite outside == 0


def test_fov_filter_drops_absent_needs_an_absent_picture_to_drop():
    """No EXIF-less picture in the baseline is no evidence either way."""
    fetcher = _fetcher(
        [_search_response([A360]), _search_response([A360]), _search_response([A360])]
    )
    row = pf.stage_access(_city(), fetcher)
    assert row["fov_can_answer"] is False
    assert row["fov_filter_drops_absent"] is False


def test_summarize_access_counts_only_the_cities_that_can_answer():
    """
    A behaviour that held in one city and not another is the interesting
    result, so this counts cities rather than flattening to a boolean -- and
    it counts them out of the cities that CAN answer. Pooling the
    unanswerable ones in reports agreement that was never measured, which is
    how "20 of 20" got written for a question one of the twenty could not be
    asked.
    """

    def city(paginates, honoured, drops, *, can_answer_dt, can_answer_fov):
        return {
            "search_paginates": paginates,
            "datetime_can_answer": can_answer_dt,
            "datetime_filter_honoured": honoured,
            "fov_can_answer": can_answer_fov,
            "fov_filter_drops_absent": drops,
        }

    access = {
        "requests_spent": 12,
        "cities": [
            city(False, False, True, can_answer_dt=True, can_answer_fov=True),
            city(False, True, True, can_answer_dt=True, can_answer_fov=True),
            # No EXIF-less picture in the baseline, and nothing older than the
            # window: neither question can be asked here, so it is neither a
            # yes nor a no for either.
            city(False, False, False, can_answer_dt=False, can_answer_fov=False),
        ],
    }
    summary = pf.summarize_access(access)
    assert summary["n"] == 3
    assert summary["cities_where_search_paginates"] == 0
    assert summary["cities_that_can_answer_the_datetime_question"] == 2
    assert summary["cities_where_datetime_filter_honoured"] == 1
    assert summary["cities_with_an_absent_picture_in_baseline"] == 2
    assert summary["cities_where_fov_filter_drops_absent"] == 2


# ── The stages end to end, over synthetic tiles ────────────────────────────


class _TileFetcher:
    """A `Fetcher` stand-in serving tiles from a dict keyed (zoom, x, y)."""

    def __init__(self, tiles, raise_for=None):
        self.tiles = tiles
        self.raise_for = raise_for or {}
        self.requests_spent = 0
        self.empty_tiles = 0
        self.served = []

    def get_tile(self, template, zoom, x, y):
        self.requests_spent += 1
        self.served.append((zoom, x, y))
        if (zoom, x, y) in self.raise_for:
            raise self.raise_for[(zoom, x, y)]
        raw = self.tiles.get((zoom, x, y), b"")
        if not raw:
            self.empty_tiles += 1
        return raw


def _hex_feature(hex_id, lon, lat, radius, **counters):
    return {"id": hex_id, "ring": _hex_ring(lon, lat, radius), **counters}


def test_stage_screen_v2_counts_a_hex_that_overlaps_the_city_but_is_centred_outside_it():
    """
    The corrected screen, end to end: tile enumeration over the grown bbox,
    the merge across tiles, overlap selection and the sum. The one hex here is
    far larger than the city and centred outside it -- a screen hex is often
    bigger than a city bbox -- and a far-away hex in the same tile is not
    counted. The row must say 1 cell and the hex's counters.
    """
    city = _city()
    zoom = pf.SCREEN_ZOOM
    x, y = _seattle_tile(zoom)
    hexes = [
        _hex_feature(
            "86a", -122.30, 47.65, 0.05, nb_pictures=9, nb_360_pictures=7, nb_flat_pictures=2
        ),
        _hex_feature(
            "86b", -121.50, 47.65, 0.05, nb_pictures=500, nb_360_pictures=0, nb_flat_pictures=500
        ),
    ]
    fetcher = _TileFetcher({(zoom, x, y): encode_polygons("grid", hexes, x, y, zoom)})
    out = pf.stage_screen([city], fetcher, "v2_h3")
    (row,) = out["cities"]
    assert row["cells"] == 1
    assert row["screen_pictures_upper_bound"] == 9
    assert row["screen_360_upper_bound"] == 7
    assert row["screen_flat_upper_bound"] == 2
    assert out["variant"] == "v2_h3" and out["cell_deg"] is None
    assert out["requests_spent"] == out["tiles"] == len(set(fetcher.served))
    # The hex's centre is outside the city bbox, so centre selection would have
    # screened this city ZERO -- the failure the design cannot tolerate.
    assert not pf.bbox_contains(-122.30, 47.65, tuple(city["bbox"]))


def test_stage_screen_v1_sums_the_lattice_cells_within_one_cell_of_the_city():
    city = _city()
    zoom = pf.SCREEN_ZOOM
    x, y = _seattle_tile(zoom)
    cells = [
        {"lon": -122.3, "lat": 47.6, "nb_pictures": 4, "nb_360_pictures": 4, "nb_flat_pictures": 0},
        {"lon": -122.4, "lat": 47.5, "nb_pictures": 1, "nb_360_pictures": 0, "nb_flat_pictures": 1},
        {
            "lon": -121.0,
            "lat": 47.6,
            "nb_pictures": 800,
            "nb_360_pictures": 0,
            "nb_flat_pictures": 800,
        },
    ]
    fetcher = _TileFetcher({(zoom, x, y): encode_points("grid", cells, x, y, zoom)})
    (row,) = pf.stage_screen([city], fetcher, "v1_lattice")["cities"]
    assert row["cells"] == 2
    assert row["screen_pictures_upper_bound"] == 5


def test_stage_screen_shares_tiles_between_neighbouring_cities_and_prices_a_dry_run():
    """One request per DISTINCT z6 tile: two cities on one tile cost one."""
    neighbours = [
        _city("a"),
        {**_city("b"), "bbox": list(dm.grid_bbox(47.7, -122.30, 5000, 5000, 20))},
    ]
    planned = pf.stage_screen(neighbours, None)
    assert planned["planned_requests"] == planned["tiles"] == 1
    assert planned["cities"] == []
    fetcher = _TileFetcher({})
    out = pf.stage_screen(neighbours, fetcher)
    assert fetcher.requests_spent == 1
    assert [row["screen_pictures_upper_bound"] for row in out["cities"]] == [0, 0]


def _tile_centre(x, y, zoom):
    return dm.tile_frac_to_lonlat(x + 0.5, y + 0.5, zoom)


def test_measure_city_takes_a_seam_hex_once_and_scales_a_truncated_city():
    """
    Every z14 tile of the city carries a clipped piece of ONE hex, all under
    the same id with the same whole-hex counter. The count must be that
    counter once -- not once per tile -- the unioned centre must land inside
    the bbox, and a run cut short at 5 tiles must scale by tiles_total/5.
    """
    city = _city()
    zoom = pf.MEASURE_ZOOM
    tiles = dm.tiles_for_bbox(*city["bbox"], zoom)
    assert len(tiles) > 5
    served = {}
    for x, y in tiles:
        lon, lat = _tile_centre(x, y, zoom)
        piece = _hex_feature(
            "8b1",
            lon,
            lat,
            0.0004,
            nb_pictures=40,
            nb_360_pictures=30,
            nb_flat_pictures=10,
            date="2026-02-01",
        )
        served[(zoom, x, y)] = encode_polygons("grid", [piece], x, y, zoom)

    complete = pf.measure_city(city, _TileFetcher(served), max_tiles=len(tiles), seed=316)
    assert complete["complete"] is True
    assert complete["tiles_probed"] == complete["tiles_total"] == len(tiles)
    assert (complete["hexes"], complete["pictures"]) == (1, 40)
    assert (complete["pictures_360"], complete["pictures_flat"]) == (30, 10)
    assert complete["pictures_scaled_to_bbox"] == 40
    assert complete["newest_hex_date"] == "2026-02-01"

    fetcher = _TileFetcher(served)
    truncated = pf.measure_city(city, fetcher, max_tiles=5, seed=316)
    assert truncated["complete"] is False
    assert truncated["tiles_probed"] == fetcher.requests_spent == 5
    assert truncated["pictures"] == 40
    assert truncated["pictures_scaled_to_bbox"] == round(40 * len(tiles) / 5)
    # The same seed visits the same five tiles: a truncated city is reproducible.
    again = _TileFetcher(served)
    pf.measure_city(city, again, max_tiles=5, seed=316)
    assert again.served == fetcher.served


def _picture(picture_id, lon, lat, **props):
    return {"id": picture_id, "lon": lon, "lat": lat, **props}


def _serve_pictures(pictures_by_tile, zoom):
    return {
        (zoom, x, y): encode_points("pictures", points, x, y, zoom)
        for (x, y), points in pictures_by_tile.items()
    }


def test_detail_city_keeps_in_bbox_pictures_once_and_reports_types_months_and_contributors():
    city = _city()
    bbox = tuple(city["bbox"])
    zoom = pf.DETAIL_ZOOM
    tiles = dm.tiles_for_bbox(*bbox, zoom)
    # Tile A is the westernmost tile of the centre row: it straddles the
    # bbox's west edge, so it holds both in-bbox points and a point west of
    # the bbox -- the ordinary shape of an out-of-bbox row, since tile
    # columns overhang the rectangle. Tile B is its eastern neighbour.
    centre_lat = (bbox[1] + bbox[3]) / 2
    _, yc = pf._tile_xy((bbox[0] + bbox[2]) / 2, centre_lat, zoom)
    tile_a = (min(x for x, _ in tiles), yc)
    tile_b = (tile_a[0] + 1, yc)
    assert tile_a in tiles and tile_b in tiles
    outside = (bbox[0] - 1e-4, centre_lat)
    in_a = (bbox[0] + 1e-4, centre_lat)
    assert pf._tile_xy(*outside, zoom) == tile_a and pf._tile_xy(*in_a, zoom) == tile_a
    in_b = _tile_centre(*tile_b, zoom)
    assert pf.bbox_contains(*in_b, bbox)
    by_tile = {
        tile_a: [
            _picture(
                "a",
                *in_a,
                ts="2025-11-02 00:24:37+00",
                type="equirectangular",
                account_id="u1",
                first_sequence="s1",
            ),
            _picture(
                "b",
                in_a[0] + 1e-5,
                in_a[1],
                ts="2024-01-05 09:00:00+00",
                type="flat",
                account_id="u1",
                first_sequence="s1",
            ),
            _picture(
                "c",
                in_a[0] + 2e-5,
                in_a[1],
                ts=None,
                type=None,
                account_id="u2",
                first_sequence=None,
            ),
            _picture(
                "dup",
                in_a[0] + 3e-5,
                in_a[1],
                ts="2024-01-09 09:00:00+00",
                type="flat",
                account_id="u2",
                first_sequence="s2",
            ),
            _picture(
                "z",
                *outside,
                ts="2020-01-01 00:00:00+00",
                type="flat",
                account_id="u3",
                first_sequence="s3",
            ),
        ],
        # The seam copy of `dup`, as a tile buffer would carry it.
        tile_b: [
            _picture(
                "dup",
                *in_b,
                ts="2024-01-09 09:00:00+00",
                type="flat",
                account_id="u2",
                first_sequence="s2",
            ),
        ],
    }
    row = pf.detail_city(city, _TileFetcher(_serve_pictures(by_tile, zoom)), len(tiles), 316)
    assert row["complete"] is True
    assert row["pictures"] == 4  # a, b, c, dup -- z is outside, dup once
    assert (row["pictures_360"], row["pictures_flat"], row["pictures_type_absent"]) == (1, 2, 1)
    assert row["undated_pictures"] == 1
    assert row["capture_months"] == {"2024-01": 2, "2025-11": 1}
    assert row["distinct_capture_months"] == 2
    assert row["distinct_contributors"] == 2
    assert row["top_contributor_share"] == pytest.approx(0.5)
    assert row["distinct_sequences"] == 2


def _measure_args(tmp_path, *extra):
    return pf.parse_args(["--stage", "measure", "--raw-dir", str(tmp_path), *extra])


def test_the_measure_loop_reuses_a_matching_row_and_records_a_failed_city():
    """
    The loop's two non-happy paths, which the per-function tests cannot see:
    a reusable prior row is placed in the CURRENT group and counted, and a
    city whose fetch fails is recorded under `failed` rather than dropped
    (an omitted city silently shrinks a group's denominator).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cities = [_city("kept"), _city("broken")]
        by_id = pf._cities_by_id(cities)
        n = len(dm.tiles_for_bbox(*cities[0]["bbox"], pf.MEASURE_ZOOM))
        pf.write_raw(tmp, "measure", _prior("kept", tiles_total=n, tiles_probed=n))
        args = _measure_args(tmp, "--city", "kept", "--city", "broken", "--reuse-measured")
        zoom = pf.MEASURE_ZOOM
        first_tile = pf.shuffled_tiles(dm.tiles_for_bbox(*cities[1]["bbox"], zoom), 316)[0]
        fetcher = _TileFetcher({}, raise_for={(zoom, *first_tile): RuntimeError("boom")})

        out = pf._run_stage(args, cities, by_id, fetcher)

    assert [row["city_id"] for row in out["leaders"]] == ["kept"]
    assert out["leaders"][0]["reused_from"].startswith("python scripts/panoramax_feasibility.py")
    assert out["reused_rows"] == 1
    assert out["failed"] == [{"group": "leaders", "city_id": "broken", "error": "boom"}]
    assert out["typical"] == out["controls"] == []
    # The reused city cost nothing; the broken one cost exactly its first tile.
    assert out["requests_spent"] == fetcher.requests_spent == 1


def test_a_refusal_inside_the_measure_loop_stops_the_run_rather_than_being_recorded(tmp_path):
    cities = [_city("a"), _city("b")]
    args = _measure_args(tmp_path, "--city", "a", "--city", "b")
    zoom = pf.MEASURE_ZOOM
    first_tile = pf.shuffled_tiles(dm.tiles_for_bbox(*cities[0]["bbox"], zoom), 316)[0]
    fetcher = _TileFetcher({}, raise_for={(zoom, *first_tile): pf.BlockedError("429")})
    with pytest.raises(pf.BlockedError):
        pf._run_stage(args, cities, pf._cities_by_id(cities), fetcher)
    assert fetcher.requests_spent == 1  # city b was never started


def test_the_measure_dry_run_prices_each_group_under_the_tile_cap(tmp_path):
    cities = [_city("a")]
    n = len(dm.tiles_for_bbox(*cities[0]["bbox"], pf.MEASURE_ZOOM))
    args = _measure_args(tmp_path, "--city", "a", "--dry-run", "--max-tiles-per-city", "3")
    out = pf._run_stage(args, cities, pf._cities_by_id(cities), None)
    assert n > 3
    assert out["planned_requests"] == {"leaders": 3, "typical": 0, "controls": 0}
    assert out["planned_total"] == 3


def test_the_detail_targets_add_the_richest_cities_that_fit_at_z15(tmp_path):
    """
    None of the richest cities fits under --max-tiles-per-city at z15, so the
    cross-check needs a second set: the richest cities whose measure row is
    complete AND whose whole z15 tile set fits. A rich-but-truncated row and a
    city no longer in the catalog are both skipped.
    """
    big = {**_city("big"), "bbox": list(dm.grid_bbox(47.6, -122.33, 20000, 20000, 20))}
    small_a = {**_city("small-a"), "bbox": list(dm.grid_bbox(47.9, -122.33, 1500, 1500, 20))}
    small_b = {**_city("small-b"), "bbox": list(dm.grid_bbox(48.1, -122.33, 1500, 1500, 20))}
    cut = {**_city("cut"), "bbox": list(dm.grid_bbox(48.3, -122.33, 1500, 1500, 20))}
    by_id = pf._cities_by_id([big, small_a, small_b, cut])
    assert len(dm.tiles_for_bbox(*big["bbox"], pf.DETAIL_ZOOM)) > 50
    assert len(dm.tiles_for_bbox(*small_a["bbox"], pf.DETAIL_ZOOM)) <= 50

    def row(city_id, pictures, complete=True):
        return {"city_id": city_id, "pictures": pictures, "complete": complete}

    pf.write_raw(
        str(tmp_path),
        "measure",
        {
            "leaders": [row("big", 10_000), row("cut", 900, complete=False), row("gone", 800)],
            "typical": [row("small-a", 500), row("small-b", 20), row("empty", 0)],
            "controls": [],
        },
    )
    args = pf.parse_args(
        [
            "--stage",
            "detail",
            "--raw-dir",
            str(tmp_path),
            "--detail-cities",
            "1",
            "--cross-check-cities",
            "1",
            "--max-tiles-per-city",
            "50",
        ]
    )
    assert pf._detail_targets(args, by_id) == ["big", "small-a"]
    args.cross_check_cities = 5
    assert pf._detail_targets(args, by_id) == ["big", "small-a", "small-b"]
    args.cross_check_cities = 0
    assert pf._detail_targets(args, by_id) == ["big"]


def test_instances_city_attributes_pictures_to_instances_and_flags_the_search_cap():
    zoom = pf.DETAIL_ZOOM
    x, y = _seattle_tile(zoom)
    tile = encode_points(
        "pictures",
        [{"lon": -122.33, "lat": 47.60, "id": "a", "ts": "2025-06-01 00:00:00+00", "type": "flat"}],
        x,
        y,
        zoom,
    )
    far = _search_feature("far", None, lon=2.35, lat=48.86)
    far["links"] = [{"rel": "via", "instance_name": "OSM-FR", "href": "https://osm.invalid"}]
    fetcher = _fetcher([_search_response([A360, far]), _FakeResponse(200, tile)])
    row = pf.instances_city(_city(), fetcher, search_limit=2, reconcile_tiles=1)
    assert row["sampled"] == 2
    assert row["at_search_limit"] is True
    assert row["instances"] == {"IGN": 1, "OSM-FR": 1}
    assert (row["fov_360"], row["fov_flat"], row["fov_absent"]) == (1, 0, 1)
    # `a` says 360 in search but `flat` in the tile: the table keeps both
    # readings side by side, and the unfetched picture says so.
    assert row["reconciliation"]["table"] == {"360__flat": 1, "absent__not_in_fetched_tiles": 1}
    assert row["reconciliation"]["tiles_fetched"] == 1
    assert fetcher.session.calls[0][1]["limit"] == 2


def test_federation_snapshot_reads_the_providers_misspelled_harvest_key():
    """
    The instances endpoint spells it `last_succesful_harvest`. Reading the
    correctly spelled key would silently record every instance as never
    harvested, which is the kind of null that looks like a finding.
    """
    instances = {
        "instances": [
            {"name": "small", "url": "https://s.invalid", "last_succesful_harvest": "2026-09-01"},
            {"name": "IGN", "url": "https://ign.invalid", "last_succesful_harvest": "2026-09-04"},
        ]
    }
    stats = {
        "generic_stats": {"nb_pictures": 105},
        "stats_by_instance": {"IGN": {"nb_pictures": 100, "nb_contributors": 9}},
    }
    fetcher = _fetcher([_FakeResponse(200, b"{}", instances), _FakeResponse(200, b"{}", stats)])
    snapshot = pf.federation_snapshot(fetcher)
    assert snapshot["registered_instances"] == 2
    assert snapshot["generic_stats"] == {"nb_pictures": 105}
    assert [i["name"] for i in snapshot["instances"]] == ["IGN", "small"]  # richest first
    assert snapshot["instances"][0]["last_successful_harvest"] == "2026-09-04"
    assert snapshot["instances"][0]["nb_contributors"] == 9
    assert snapshot["instances"][1]["nb_pictures"] is None  # unreported, never 0


def test_summarize_instances_sums_the_reconciliation_and_names_the_absent_cells():
    def city(table, absent):
        return {
            "sampled": sum(table.values()),
            "at_search_limit": False,
            "instances": {"osm-fr": sum(table.values())},
            "fov_360": table.get("360__equirectangular", 0),
            "fov_flat": table.get("flat__flat", 0),
            "fov_absent": absent,
            "reconciliation": {"table": table},
        }

    instances = {
        "requests_spent": 4,
        "cities": [
            city({"360__equirectangular": 3, "absent__flat": 2}, absent=2),
            city({"absent__flat": 1, "absent__not_in_fetched_tiles": 4}, absent=5),
        ],
    }
    summary = pf.summarize_instances(instances)
    assert summary["reconciliation_table"] == {
        "360__equirectangular": 3,
        "absent__flat": 3,
        "absent__not_in_fetched_tiles": 4,
    }
    # Unlooked-up pictures are not evidence of a type, so they stay out of both.
    assert summary["absent_pictures_looked_up_in_tiles"] == 3
    assert summary["absent_pictures_typed_flat_in_tiles"] == 3
    assert summary["fov_absent_share"] == pytest.approx(7 / 10)


def test_compose_catalog_scales_the_typical_draw_and_counts_the_leaders_exactly():
    """
    The gate over the whole catalog: zeros exact, leaders exact, and the rest
    of the positive stratum through the uniform `typical` draw. 100 cities:
    60 screened zero, 40 positive, 5 leaders measured, so the typical stratum
    is 35; a 4-city typical draw with 2 at >= 100 puts half the stratum there.
    """
    screen = {
        "cities": [{"screen_pictures_upper_bound": 0}] * 60
        + [{"screen_pictures_upper_bound": 9}] * 40
    }
    measure = {
        "leaders": [{"pictures_scaled_to_bbox": n} for n in (50_000, 20_000, 5_000, 500, 150)],
        "typical": [{"pictures_scaled_to_bbox": n} for n in (0, 3, 120, 4_000)],
        "controls": [],
    }
    out = pf.compose_catalog(screen, measure)
    assert (out["screened_zero_exact"], out["typical_stratum_size"]) == (60, 35)
    assert out["median_city_pictures"] == 0
    at = out["at_or_above"]
    assert at["1"]["estimated_catalog_cities"] == 5 + round(0.75 * 35)
    assert at["100"]["estimated_catalog_cities"] == 5 + round(0.5 * 35)
    assert at["1000"]["estimated_catalog_cities"] == 3 + round(0.25 * 35)
    assert at["10000"]["estimated_catalog_cities"] == 2 + 0
    assert at["10000"]["estimated_catalog_share"] == pytest.approx(0.02)
    assert pf.compose_catalog(None, measure) is None
    assert pf.compose_catalog(screen, None) is None
    # Fewer than half screened zero: the median is not known to be zero.
    half = {"cities": screen["cities"][:40] + screen["cities"][60:]}
    assert pf.compose_catalog(half, measure)["median_city_pictures"] is None


def test_summarize_measure_keeps_the_three_groups_apart():
    def row(city_id, pictures, complete=True):
        return {
            "city_id": city_id,
            "pictures": pictures,
            "pictures_360": pictures,
            "pictures_flat": 0,
            "pictures_scaled_to_bbox": pictures * (1 if complete else 2),
            "complete": complete,
        }

    measure = {
        "requests_spent": 5,
        "empty_tiles": 1,
        "failed": [],
        "reused_rows": 2,
        "leaders": [row("l1", 1000, complete=False), row("l2", 900)],
        "typical": [row("t1", 3), row("t2", 0)],
        "controls": [row("c1", 0)],
    }
    summary = pf.summarize_measure(measure)
    assert summary["reused_rows"] == 2
    assert summary["leaders"]["n"] == 2 and summary["leaders"]["cities_truncated"] == 1
    assert summary["leaders"]["picture_count_scaled_distribution"]["max"] == 2000
    assert summary["typical"]["cities_with_any_pictures"] == 1
    assert summary["typical"]["picture_count_distribution"]["p50"] == pytest.approx(1.5)
    assert summary["controls"]["pictures_total"] == 0


# ── One --city resolver, and no stage that writes an empty run ─────────────

CITY_STAGES = ("measure", "detail", "instances", "access")


def _stage_args(tmp_path, stage, *extra):
    return pf.parse_args(["--stage", stage, "--raw-dir", str(tmp_path), *extra])


@pytest.mark.parametrize("stage", CITY_STAGES)
def test_an_unknown_city_id_stops_every_stage_before_it_fetches_or_writes(stage, tmp_path):
    """
    `--city` used to be resolved in three places that disagreed, and BOTH
    answers were wrong. `_measure_targets` and `_detail_targets` filtered
    unknown ids out, so a typo produced an empty stage that spent nothing,
    logged success and overwrote a real run's artifact; `instances` and
    `access` skipped the filter and died on a KeyError. One resolver now, one
    answer, and it is the loud one.
    """
    cities = [_city("real--city")]
    args = _stage_args(tmp_path, stage, "--city", "reall--city")
    fetcher = _TileFetcher({})
    with pytest.raises(SystemExit) as exit_info:
        pf._run_stage(args, cities, pf._cities_by_id(cities), fetcher)
    assert exit_info.value.code == pf.USAGE_EXIT == 64
    assert fetcher.requests_spent == 0
    assert not os.listdir(tmp_path)


@pytest.mark.parametrize("stage", CITY_STAGES)
def test_a_known_city_id_still_reaches_every_stage(stage, tmp_path):
    """The resolver must not have broken the case it exists to serve."""
    cities = [_city("real--city")]
    args = _stage_args(tmp_path, stage, "--city", "real--city", "--dry-run")
    out = pf._run_stage(args, cities, pf._cities_by_id(cities), None)
    named = out.get("cities") if "cities" in out else out["leaders"]
    assert named == ["real--city"]


def test_the_unknown_id_message_names_the_typo_and_not_the_whole_catalog(tmp_path, caplog):
    cities = [_city("a--b"), _city("c--d")]
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        pf.resolve_city_override(["a--b", "zzz--qq"], pf._cities_by_id(cities))
    assert "zzz--qq" in caplog.text
    assert "a--b" not in caplog.text


def test_a_measure_run_that_resolves_to_no_cities_refuses_to_overwrite_its_artifact(tmp_path):
    """
    Every stage writes its artifact unconditionally when it finishes, so a
    zero-city run is not a harmless no-op -- it is a successful-looking
    overwrite of whatever that stage measured last time.
    """
    screen = {"cities": [], "requests_spent": 0}
    pf.write_raw(str(tmp_path), "screen", screen)
    real = pf.write_raw(str(tmp_path), "measure", _prior("kept"))
    before = open(real, encoding="utf-8").read()

    args = _stage_args(tmp_path, "measure")
    with pytest.raises(SystemExit) as exit_info:
        pf._run_stage(args, [], {}, _TileFetcher({}))
    assert exit_info.value.code == 64
    assert open(real, encoding="utf-8").read() == before


def test_a_detail_run_with_no_positive_measure_rows_refuses_rather_than_emptying_it(tmp_path):
    pf.write_raw(
        str(tmp_path),
        "measure",
        {
            "leaders": [{"city_id": "a", "pictures": 0, "complete": True}],
            "typical": [],
            "controls": [],
        },
    )
    real = pf.write_raw(str(tmp_path), "detail", {"cities": [{"city_id": "a"}]})
    before = open(real, encoding="utf-8").read()
    args = _stage_args(tmp_path, "detail")
    with pytest.raises(SystemExit) as exit_info:
        pf._run_stage(args, [_city("a")], pf._cities_by_id([_city("a")]), _TileFetcher({}))
    assert exit_info.value.code == 64
    assert open(real, encoding="utf-8").read() == before


def test_a_missing_upstream_stage_is_a_usage_error_not_a_bare_exit(tmp_path):
    """Exit 64 like every other bad invocation here, never 1."""
    for stage in ("measure", "detail"):
        with pytest.raises(SystemExit) as exit_info:
            pf._run_stage(_stage_args(tmp_path, stage), [], {}, _TileFetcher({}))
        assert exit_info.value.code == 64


# ── A killed stage costs one city, not the run ─────────────────────────────


def test_write_raw_replaces_atomically_and_leaves_no_staging_file(tmp_path):
    """
    A process killed midway through a write must not be able to leave a
    half-written file where a complete one was -- the posture
    `catalog-backups.md` argues for the catalog, for the same reason.
    """
    path = pf.write_raw(str(tmp_path), "screen", {"cities": [1, 2, 3]})
    pf.write_raw(str(tmp_path), "screen", {"cities": [4]})
    assert json.load(open(path, encoding="utf-8")) == {"cities": [4]}
    assert sorted(os.listdir(tmp_path)) == ["screen.json"]


def _spread_cities(*ids):
    """
    Cities on DISTINCT bboxes, one z15 tile apart.

    `_city()` gives every id the same rectangle, which is fine for the pure
    functions and wrong for anything walking a city loop: a tile rigged to
    fail for the third city fails on the first one too, and the test then
    passes or fails for a reason that has nothing to do with the code.
    """
    return [
        {**_city(city_id), "bbox": list(dm.grid_bbox(47.60 + 0.05 * n, -122.33, 300, 300, 20))}
        for n, city_id in enumerate(ids)
    ]


def test_a_stage_killed_midway_leaves_its_finished_cities_in_a_partial_file(tmp_path):
    """
    THE `finally` THAT WOULD NOT HAVE HELPED. The first `detail` run of this
    study was killed by a low-memory sweep two cities in and lost ~400 paced
    requests. A SIGKILL runs no handler and unwinds no stack, so the only
    thing that survives it is bytes already on disk: every stage that walks
    cities saves after each one, and this pins that the saved bytes are the
    finished cities and nothing else.
    """
    cities = _spread_cities("a", "b", "c")
    by_id = pf._cities_by_id(cities)
    zoom = pf.DETAIL_ZOOM
    doomed = pf.shuffled_tiles(dm.tiles_for_bbox(*cities[2]["bbox"], zoom), 316)[0]
    survivors = {tile for city in cities[:2] for tile in dm.tiles_for_bbox(*city["bbox"], zoom)}
    assert doomed not in survivors, "the rigged tile must belong only to the third city"
    fetcher = _TileFetcher({}, raise_for={(zoom, *doomed): RuntimeError("OOM-killed")})
    args = _stage_args(
        tmp_path,
        "detail",
        "--city",
        "a",
        "--city",
        "b",
        "--city",
        "c",
        "--max-tiles-per-city",
        "1",
    )
    progress = pf.StageProgress(str(tmp_path), "detail", "python ... --stage detail")

    with pytest.raises(RuntimeError, match="OOM-killed"):
        pf._run_stage(args, cities, by_id, fetcher, progress)

    saved = json.load(open(pf.partial_path(str(tmp_path), "detail"), encoding="utf-8"))
    assert [row["city_id"] for row in saved["cities"]] == ["a", "b"]
    assert saved["_partial"] is True
    assert saved["_measured_by"] == "python ... --stage detail"
    # And the canonical artifact is untouched, because it never existed.
    assert not os.path.exists(pf.raw_path(str(tmp_path), "detail"))


def test_a_partial_never_clobbers_the_finished_run_it_is_re_running(tmp_path):
    """
    Checkpointing INTO the artifact would have traded one data-loss mode for
    another: re-running `detail` and losing it after one city would replace a
    complete run with a one-city one. The partial lives beside it instead.
    """
    finished = pf.write_raw(
        str(tmp_path), "detail", {"cities": [{"city_id": f"old-{n}"} for n in range(20)]}
    )
    before = open(finished, encoding="utf-8").read()
    cities = _spread_cities("a", "b")
    zoom = pf.DETAIL_ZOOM
    doomed = pf.shuffled_tiles(dm.tiles_for_bbox(*cities[1]["bbox"], zoom), 316)[0]
    fetcher = _TileFetcher({}, raise_for={(zoom, *doomed): RuntimeError("boom")})
    args = _stage_args(
        tmp_path, "detail", "--city", "a", "--city", "b", "--max-tiles-per-city", "1"
    )
    progress = pf.StageProgress(str(tmp_path), "detail", "stamp")

    with pytest.raises(RuntimeError):
        pf._run_stage(args, cities, pf._cities_by_id(cities), fetcher, progress)

    assert open(finished, encoding="utf-8").read() == before
    partial = json.load(open(pf.partial_path(str(tmp_path), "detail"), encoding="utf-8"))
    assert [row["city_id"] for row in partial["cities"]] == ["a"]


def test_a_finished_stage_commits_and_drops_the_partial(tmp_path):
    """A leftover *.partial.json therefore always means an unfinished run."""
    progress = pf.StageProgress(str(tmp_path), "detail", "stamp")
    progress.save({"cities": [{"city_id": "a"}]})
    assert progress.saved_path and os.path.exists(progress.saved_path)

    committed = progress.commit(
        {"cities": [{"city_id": "a"}, {"city_id": "b"}], "_measured_by": "s"}
    )
    assert committed == pf.raw_path(str(tmp_path), "detail")
    assert not os.path.exists(pf.partial_path(str(tmp_path), "detail"))
    assert progress.saved_path is None
    assert len(json.load(open(committed, encoding="utf-8"))["cities"]) == 2


@pytest.mark.parametrize("stage", ["measure", "instances", "access"])
def test_every_city_walking_stage_checkpoints_and_not_only_detail(stage, tmp_path):
    """
    `detail` is the stage that actually lost a run, but `measure` is 3,321
    requests and `instances` and `access` are paced against the same host, so
    the checkpoint belongs to the shape and not to the one incident.

    Counted at CALL time, never from the captured payloads: the stages hand
    `save` the live list they are still appending to, so a test holding
    references would read every checkpoint as the final one and pass even if
    the stage saved only once, at the end.
    """
    sizes = []
    progress = pf.StageProgress(str(tmp_path), stage, "stamp")
    key = "leaders" if stage == "measure" else "cities"
    progress.save = lambda payload: sizes.append(len(payload[key]))  # noqa: E731
    cities = _spread_cities("a", "b")
    by_id = pf._cities_by_id(cities)
    args = _stage_args(tmp_path, stage, "--city", "a", "--city", "b")
    if stage == "measure":
        pf._run_stage(args, cities, by_id, _TileFetcher({}), progress)
    else:
        empty = _search_response([])
        stats = _FakeResponse(200, b"{}", {})
        responses = [stats, stats] + [empty] * 2 if stage == "instances" else [empty] * 6
        pf._run_stage(args, cities, by_id, _fetcher(responses), progress)
    assert sizes == [1, 2]


def test_a_dry_run_and_a_bare_progress_write_nothing(tmp_path):
    """--dry-run spends nothing and must leave nothing behind either."""
    disabled = pf.StageProgress()
    assert disabled.save({"cities": []}) is None
    assert disabled.commit({"cities": []}) is None
    assert not os.listdir(tmp_path)


def _catalog(tmp_path, *city_ids):
    """A minimal frozen-grid catalog on disk, for the `main` entry point."""
    db = tmp_path / "catalog.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE cities (city_id TEXT PRIMARY KEY, display_name TEXT, country_name TEXT, "
        "center_lat REAL, center_lon REAL, grid_width_m INTEGER, grid_height_m INTEGER, "
        "step_m INTEGER, enabled INTEGER)"
    )
    connection.executemany(
        "INSERT INTO cities VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (city_id, city_id.upper(), "United States", 47.60 + 0.05 * n, -122.33, 300, 300, 20, 1)
            for n, city_id in enumerate(city_ids)
        ],
    )
    connection.commit()
    connection.close()
    return db


def _main_argv(tmp_path, *extra):
    return [
        "--stage",
        "detail",
        "--db",
        str(_catalog(tmp_path, "a", "b")),
        "--raw-dir",
        str(tmp_path),
        "--city",
        "a",
        "--city",
        "b",
        "--max-tiles-per-city",
        "1",
        "--rate",
        "0",
        *extra,
    ]


def test_main_returns_the_blocked_exit_code_and_keeps_what_it_measured(tmp_path, monkeypatch):
    """
    The wiring `_run_stage` tests cannot see: a refusal is exit 75 (the repo's
    blocked family), the cities that finished are on disk, and the artifact of
    the run being re-run is left exactly as it was.
    """
    finished = pf.write_raw(str(tmp_path), "detail", {"cities": [{"city_id": "old"}]})
    before = open(finished, encoding="utf-8").read()

    calls = {"n": 0}

    class _BlockingFetcher(_TileFetcher):
        def __init__(self, *args, **kwargs):
            super().__init__({})

        def get_tile(self, template, zoom, x, y):
            calls["n"] += 1
            if calls["n"] > 1:
                raise pf.BlockedError("HTTP 429")
            return super().get_tile(template, zoom, x, y)

    monkeypatch.setattr(pf, "Fetcher", _BlockingFetcher)
    assert pf.main(_main_argv(tmp_path)) == 75

    assert open(finished, encoding="utf-8").read() == before
    partial = json.load(open(pf.partial_path(str(tmp_path), "detail"), encoding="utf-8"))
    assert [row["city_id"] for row in partial["cities"]] == ["a"]
    assert partial["_measured_by"].startswith("python scripts/panoramax_feasibility.py")


def test_main_commits_a_finished_stage_and_leaves_no_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "Fetcher", lambda *a, **k: _TileFetcher({}))
    assert pf.main(_main_argv(tmp_path)) == 0
    written = json.load(open(pf.raw_path(str(tmp_path), "detail"), encoding="utf-8"))
    assert [row["city_id"] for row in written["cities"]] == ["a", "b"]
    assert not os.path.exists(pf.partial_path(str(tmp_path), "detail"))


def test_main_exits_64_on_an_unknown_city_without_sending_or_writing_anything(
    tmp_path, monkeypatch
):
    """
    Exit 64 through the real entry point, and the two properties that matter
    with it: not one request went out, and the raw directory holds only the
    catalog it was handed. (A `requests.Session` IS constructed first, which
    opens no socket -- the guard is on traffic and on bytes written, not on
    object construction.)
    """
    fetcher = _TileFetcher({})
    monkeypatch.setattr(pf, "Fetcher", lambda *a, **k: fetcher)
    argv = [
        "--stage",
        "detail",
        "--db",
        str(_catalog(tmp_path, "a")),
        "--raw-dir",
        str(tmp_path),
        "--city",
        "nope",
    ]
    with pytest.raises(SystemExit) as exit_info:
        pf.main(argv)
    assert exit_info.value.code == 64
    assert fetcher.requests_spent == 0
    assert os.listdir(tmp_path) == ["catalog.db"]


# ── The screen reports measured traffic, not planned tiles ─────────────────


def test_the_screen_reports_the_traffic_it_SENT_not_the_tiles_it_planned():
    """
    `Fetcher.get` counts every ATTEMPT, so a retried 5xx sends more traffic
    than the plan priced. This is the one stage whose cost the writeup quotes
    forward as a standing recommendation -- "113 requests re-screen the whole
    catalog" -- and against a host with no documented limit the number that
    travels has to be measured rather than planned.
    """

    class _RetryingTileFetcher(_TileFetcher):
        def get_tile(self, template, zoom, x, y):
            self.requests_spent += 1  # the 503 that preceded the success
            return super().get_tile(template, zoom, x, y)

    city = _city()
    zoom = pf.SCREEN_ZOOM
    x, y = _seattle_tile(zoom)
    hexes = [
        _hex_feature(
            "86a", -122.30, 47.65, 0.05, nb_pictures=9, nb_360_pictures=7, nb_flat_pictures=2
        )
    ]
    fetcher = _RetryingTileFetcher({(zoom, x, y): encode_polygons("grid", hexes, x, y, zoom)})
    out = pf.stage_screen([city], fetcher, "v2_h3")
    assert out["tiles"] == 1
    assert out["requests_spent"] == fetcher.requests_spent == 2 > out["tiles"]


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


@pytest.mark.parametrize("stage", ["detail", "instances", "access"])
def test_every_stage_that_walks_the_detail_targets_stamps_what_selects_them(stage):
    """
    `instances` and `access` reuse `_detail_targets`, so their city set moves
    with --detail-cities, --cross-check-cities and --max-tiles-per-city just
    as `detail`'s does; a stamp that named those only for `detail` would let
    an instances record claim a default draw nobody made.
    """
    stamp = pf.measured_by(
        _args(stage=stage, detail_cities=3, cross_check_cities=2, max_tiles_per_city=50), stage
    )
    for fragment in ("--detail-cities 3", "--cross-check-cities 2", "--max-tiles-per-city 50"):
        assert fragment in stamp, stamp


def test_a_city_override_is_stamped_because_it_replaces_the_whole_selection():
    stamp = pf.measured_by(_args(stage="access", city=["x--y", "z--w"]), "access")
    assert "--city x--y --city z--w" in stamp
    assert "--city" not in pf.measured_by(_args(stage="access"), "access")


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
    for stage in ("screen", "measure", "detail", "instances", "access"):
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
        "access": pf.summarize_access,
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


# A ratio band alone is finer than the instruments' own resolution on a small
# city. The writeup's explanation for every non-exact ratio is "a picture on a
# hexagon edge assigned differently by the two geometries" -- a plus-or-minus
# ONE effect -- and on Aberdeen's 3 pictures one picture is 33%, six times a 5%
# band. So the pin is the larger of the two: 5% for cities big enough for a
# percentage to mean something, and 2 pictures for the ones where it does not.
CROSS_CHECK_RATIO_BAND = 0.05
CROSS_CHECK_ABSOLUTE_BAND = 2


def test_the_two_tile_instruments_agree_on_every_city_complete_in_both(record):
    """
    The z14 grid's counters are server-aggregated; the z15 pictures layer is
    counted here per picture. Nothing else checks the aggregate layer every
    count rests on. Measured 2026-09-05 over 8 complete cities: 5 exact, the
    rest within 3 pictures (ratios 0.9989-1.0357).
    """
    check = record["cross_check"]
    if not check:
        pytest.skip("needs cities complete in both the measure and detail stages")
    assert check["n"] >= 5, "the cross-check set is what --cross-check-cities exists for"
    for row in check["cities"]:
        grid, layer = row["z14_grid_pictures"], row["z15_pictures_layer"]
        difference = abs(layer - grid)
        assert (
            difference <= CROSS_CHECK_ABSOLUTE_BAND or difference <= CROSS_CHECK_RATIO_BAND * grid
        ), row


def test_the_cross_check_band_is_not_finer_than_one_picture_on_a_small_city(record):
    """
    The guard on the guard. A band expressed only as a ratio silently asserts
    EXACT agreement on any city small enough that one picture exceeds it, and
    three of the eight cross-check cities are that small (Aberdeen 3, Pierre 7,
    Ridgeley 28). That is a stronger claim than the study makes and one a
    re-run has no reason to keep satisfying.
    """
    check = record["cross_check"]
    if not check:
        pytest.skip("needs cities complete in both the measure and detail stages")
    small = [
        row for row in check["cities"] if row["z14_grid_pictures"] * CROSS_CHECK_RATIO_BAND < 1
    ]
    assert small, "no small city left to protect -- re-derive this band before deleting it"
    for row in small:
        # One boundary picture moving must stay inside the band for these.
        assert CROSS_CHECK_ABSOLUTE_BAND >= 1, row


def test_the_tile_type_field_had_no_absent_state_over_every_picture_seen(record):
    """#316's third state, refuted per picture rather than per total."""
    if not record["detail"]["available"]:
        pytest.skip("needs the detail stage")
    summary = record["detail"]["summary"]
    assert summary["pictures_seen"] > 0
    assert summary["pictures_type_absent"] == 0
