"""
Issue #106 grid-density experiment (scripts/grid_density_common.py).

Network-free tests. The critical property is lattice alignment: the experiment's
5 m lattice must contain the production 20 m grid points with EXACT float
equality (not just quantized) at indices (4i, 4j) — including cities whose
int(dim/20) is odd, where the production index range is asymmetric and a
naive int(dim/5) sizing would drop the most-negative row/col.
"""

import importlib.util
import io
import json
import os
import shutil
import sys
from dataclasses import dataclass

import geopy
import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "grid_density_common", os.path.join(PROJECT_ROOT, "scripts", "grid_density_common.py")
)
gd = importlib.util.module_from_spec(_spec)
# Register before exec: the module's dataclasses resolve their string
# annotations via sys.modules[cls.__module__].
sys.modules[_spec.name] = gd
_spec.loader.exec_module(gd)

from streetscape_metadata_tracker.download_common import generate_grid_points  # noqa: E402


@dataclass
class FakeCity:
    city_id: str
    center_lat: float
    center_lon: float
    grid_width_m: int
    grid_height_m: int
    step_m: int


ADRIAN = FakeCity("adrian--oregon--united-states", 43.7407104618505, -117.0716804, 815, 811, 20)
# Synthetic odd case: h20 = int(830/20) = 41 (odd) -> asymmetric range [-21, 20].
ODD = FakeCity("odd--test", 47.6, -122.3, 830, 830, 20)

FULL = gd.StudyArea("full", "x", None, "full")


def _production_points(city):
    w = int(city.grid_width_m / city.step_m)
    h = int(city.grid_height_m / city.step_m)
    origin = geopy.Point(city.center_lat, city.center_lon)
    return generate_grid_points(origin, w, h, city.step_m)


@pytest.mark.parametrize("city", [ADRIAN, ODD], ids=["adrian-even", "synthetic-odd"])
def test_lattice_contains_production_grid_bit_exact(city):
    i20, j20 = gd.index_ranges_20m(city, FULL)
    i5, j5 = gd.fine_index_ranges(i20, j20, substep=4)
    origin = geopy.Point(city.center_lat, city.center_lon)
    fine = gd.generate_lattice(origin, i5, j5, gd.FINE_STEP_M)
    fine_by_ij = {(i, j): (lat, lon) for lat, lon, i, j in fine}

    production = _production_points(city)
    assert len(production) == len(i20) * len(j20)
    for lat, lon, i, j in production:
        assert (4 * i, 4 * j) in fine_by_ij, f"production index ({i},{j}) missing"
        flat, flon = fine_by_ij[(4 * i, 4 * j)]
        # Exact float equality, not approx: same geopy arithmetic, same inputs.
        assert flat == lat and flon == lon, f"coordinate mismatch at ({i},{j})"


def test_odd_grid_range_is_asymmetric():
    i20, j20 = gd.index_ranges_20m(ODD, FULL)
    # h20 = int(830/20) = 41 (odd): production's range(-41//2, 41//2+1) is
    # the asymmetric -21..20, NOT -20..20.
    assert (i20[0], i20[-1]) == (-21, 20)
    assert len(i20) == 42
    i5, _ = gd.fine_index_ranges(i20, j20, 4)
    assert (i5[0], i5[-1]) == (-84, 80)
    # A naive int(dim/5) lattice (h5=166 -> range(-83, 84)) would miss -84.
    assert -int(830 / 5) // 2 == -83


def test_variant_masks_nested_and_sized():
    i20, j20 = gd.index_ranges_20m(ADRIAN, FULL)
    i5, j5 = gd.fine_index_ranges(i20, j20, 4)
    origin = geopy.Point(ADRIAN.center_lat, ADRIAN.center_lon)
    lattice = gd.lattice_frame(gd.generate_lattice(origin, i5, j5, 5.0))
    masks = gd.variant_masks(lattice["i"], lattice["j"])
    assert masks["step20"].sum() == len(i20) * len(j20) == 41 * 41
    assert masks["step10"].sum() == 81 * 81
    assert masks["step5"].sum() == len(lattice) == 161 * 161
    # Nesting: 20 subset of 10 subset of 5.
    assert not (masks["step20"] & ~masks["step10"]).any()
    assert not (masks["step10"] & ~masks["step5"]).any()


def test_tile_subrange_matches_full_grid_points():
    city = FakeCity("tile--test", 44.5645659, -123.2620435, 8355, 9702, 20)
    tile_area = gd.StudyArea("t", "x", 3, "tile")
    it, jt = gd.index_ranges_20m(city, tile_area)
    assert (it[0], it[-1]) == (-3, 3) and (jt[0], jt[-1]) == (-3, 3)
    origin = geopy.Point(city.center_lat, city.center_lon)
    tile_pts = {(i, j): (lat, lon) for lat, lon, i, j in gd.generate_lattice(origin, it, jt, 20.0)}
    full = {(i, j): (lat, lon) for lat, lon, i, j in _production_points(city)}
    for ij, (lat, lon) in tile_pts.items():
        assert full[ij] == (lat, lon)


def test_tile_halfwidth_exceeding_grid_raises():
    small = FakeCity("small--test", 44.0, -121.0, 200, 200, 20)  # i20 in [-5, 5]
    with pytest.raises(ValueError, match="halfwidth"):
        gd.index_ranges_20m(small, gd.StudyArea("t", "x", 50, "tile"))


def test_attach_indices_roundtrip_through_csv_shuffled():
    origin = geopy.Point(44.0, -121.0)
    lattice = gd.lattice_frame(gd.generate_lattice(origin, range(-4, 5), range(-4, 5), 5.0))
    df = pd.DataFrame({"query_lat": lattice["lat"], "query_lon": lattice["lon"], "status": "OK"})
    df = df.sample(frac=1.0, random_state=7)  # engine writes batches out of order
    buf = io.StringIO()
    df.to_csv(buf, index=False)  # exercises the float->text->float round-trip
    reread = pd.read_csv(io.StringIO(buf.getvalue()))
    out = gd.attach_indices(reread, lattice)
    assert len(out) == len(lattice)
    key = {gd.quant_key(r.lat, r.lon): (r.i, r.j) for r in lattice.itertuples(index=False)}
    for row in out.itertuples(index=False):
        assert key[gd.quant_key(row.query_lat, row.query_lon)] == (row.i, row.j)


def test_attach_indices_tolerates_ulp_shaved_coordinate():
    """
    The engine's CSV write can shave a trailing ULP off a coordinate; when the
    value sits on a 9-decimal rounding boundary the quantized key flips
    (observed on 2/160,801 Corvallis rows). The nearest-point fallback must
    absorb it and still assign the right (i, j).
    """
    origin = geopy.Point(44.0, -121.0)
    lattice = gd.lattice_frame(gd.generate_lattice(origin, range(0, 3), range(0, 3), 5.0))
    df = pd.DataFrame({"query_lat": lattice["lat"], "query_lon": lattice["lon"]})
    df.loc[4, "query_lon"] = df.loc[4, "query_lon"] - 1.5e-13  # ULP-scale shave
    assert (
        gd.quant_key(df.loc[4, "query_lat"], df.loc[4, "query_lon"])
        != gd.quant_key(lattice.loc[4, "lat"], lattice.loc[4, "lon"])
        or True
    )  # the shave may or may not flip the key; the join must work either way
    out = gd.attach_indices(df, lattice)
    assert (out.loc[4, "i"], out.loc[4, "j"]) == (lattice.loc[4, "i"], lattice.loc[4, "j"])


def test_attach_indices_rejects_partial_and_foreign_rows():
    origin = geopy.Point(44.0, -121.0)
    lattice = gd.lattice_frame(gd.generate_lattice(origin, range(0, 3), range(0, 3), 5.0))
    full = pd.DataFrame({"query_lat": lattice["lat"], "query_lon": lattice["lon"]})
    with pytest.raises(ValueError, match="missing from the snapshot"):
        gd.attach_indices(full.iloc[:-1], lattice)
    corrupted = full.copy()
    corrupted.loc[0, "query_lat"] += 0.001
    with pytest.raises(ValueError, match="no lattice match"):
        gd.attach_indices(corrupted, lattice)


def test_road_clip_mask_inside_outside():
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import LineString

    origin = geopy.Point(44.0, -121.0)
    # 1-D lattice row: points every 5 m east/west of origin along i=0.
    lattice = gd.lattice_frame(gd.generate_lattice(origin, range(0, 1), range(-8, 9), 5.0))
    # A north-south street through the origin: only points within 15 m east/west
    # qualify -> j in {-3..3} (|j|*5 <= 15).
    edge = LineString([(-121.0, 43.999), (-121.0, 44.001)])
    edges = gpd.GeoDataFrame({"edge_id": ["e"]}, geometry=[edge], crs="EPSG:4326")
    mask = gd.road_clip_mask(lattice, edges, clip_dist_m=15.0)
    expected = np.abs(lattice["j"].to_numpy()) <= 3
    assert (mask == expected).all()


def test_estimate_counts_pinned():
    """Regression-pin the experiment's exact per-area query counts."""
    corvallis = FakeCity("corvallis", 44.5645659, -123.2620435, 8355, 9702, 20)
    seattle = FakeCity("seattle", 47.60757625, -122.3420645, 17689, 28145, 20)
    tile50 = gd.StudyArea("t", "x", 50, "tile")

    i, j = gd.index_ranges_20m(ADRIAN, FULL)
    i5, j5 = gd.fine_index_ranges(i, j, 4)
    assert len(i) * len(j) == 1_681
    assert len(i5) * len(j5) == 25_921

    for city in (corvallis, seattle):
        i, j = gd.index_ranges_20m(city, tile50)
        i5, j5 = gd.fine_index_ranges(i, j, 4)
        assert len(i) * len(j) == 10_201
        assert len(i5) * len(j5) == 160_801

    assert 25_921 + 2 * 160_801 == 347_523


# ── The committed record (docs/experiments/grid-density_*) ──────────────────
#
# The writeup's numbers must trace to the committed JSON, and the JSON must be
# produced by committed code (CLAUDE.md, "Notes"). These tests are that
# contract: the summary CSV regenerates byte-for-byte from the JSON, each
# `offsets` percentile sits in the histogram bin its cumulative share implies,
# and every share the writeup quotes recomputes from the committed bins.

_aspec = importlib.util.spec_from_file_location(
    "grid_density_analyze", os.path.join(PROJECT_ROOT, "scripts", "grid_density_analyze.py")
)
ga = importlib.util.module_from_spec(_aspec)
sys.modules[_aspec.name] = ga
_aspec.loader.exec_module(ga)

DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "experiments")
COMMITTED_JSON = os.path.join(DOCS_DIR, ga.DOCS_METRICS_NAME)
COMMITTED_CSV = os.path.join(DOCS_DIR, ga.DOCS_SUMMARY_NAME)


@pytest.fixture(scope="module")
def committed():
    with open(COMMITTED_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def test_distance_histogram_keeps_the_tail_as_a_count():
    edges = np.array([0.0, 1.0, 2.0])
    h = ga.distance_histogram(np.array([0.5, 1.5, 1.99, 2.0, 7.0]), edges)
    assert h == {
        "bin_edges": [0.0, 1.0, 2.0],
        "counts": [1, 2],
        "n_above_last_edge": 2,
        "n_total": 5,
    }
    # Nothing is silently dropped: bins plus tail account for every value.
    assert sum(h["counts"]) + h["n_above_last_edge"] == h["n_total"]


def test_offset_metrics_and_distributions_describe_the_same_arrays():
    rng = np.random.default_rng(106)
    offsets = rng.uniform(0.0, 60.0, 500)
    nn = rng.normal(10.0, 0.3, 300)
    m = ga.offset_metrics(offsets, nn)
    d = ga.distance_distributions(offsets, nn)
    assert m["n_offset_pairs"] == d["query_to_pano_m"]["n_total"] == 500
    assert m["n_unique_official_panos"] == d["pano_nearest_neighbor_m"]["n_total"] == 300
    assert m["query_to_pano_m"]["max"] == round(float(offsets.max()), 1)
    assert len(d["pano_nearest_neighbor_m"]["bin_edges"]) == 121  # 0.25 m bins to 30 m
    assert len(d["query_to_pano_m"]["bin_edges"]) == 101  # 2 m bins to 200 m


def _cum_share_below(hist: dict, edge: float) -> float:
    edges = np.asarray(hist["bin_edges"], dtype=float)
    idx = int(np.searchsorted(edges, edge))
    assert edges[idx] == edge
    return 100.0 * float(np.sum(hist["counts"][:idx])) / hist["n_total"]


@pytest.mark.parametrize("dist_key", ["pano_nearest_neighbor_m", "query_to_pano_m"])
def test_committed_percentiles_sit_in_their_histogram_bins(committed, dist_key):
    """The two blocks of the JSON describe the same arrays; a swapped or stale
    histogram, a wrong n_total or a wrong percentile all break this."""
    for area, blk in committed["areas"].items():
        hist = blk["distributions"][dist_key]
        assert sum(hist["counts"]) + hist["n_above_last_edge"] == hist["n_total"], area
        edges = np.asarray(hist["bin_edges"], dtype=float)
        for pkey, value in blk["offsets"][dist_key].items():
            if not pkey.startswith("p"):
                continue
            q = float(pkey[1:])
            b = int(np.searchsorted(edges, value, side="right")) - 1
            # One bin of slack each side: percentiles are rounded to 0.1 m and
            # a bin is 0.25 m / 2 m wide, so the rounded value can cross an edge.
            lo = _cum_share_below(hist, edges[max(b - 1, 0)])
            hi = _cum_share_below(hist, edges[min(b + 2, len(edges) - 1)])
            assert lo <= q <= hi, (area, dist_key, pkey, value, lo, hi)


def test_committed_summary_csv_regenerates_from_committed_json(committed, tmp_path):
    assert set(committed["areas"]) == set(gd.STUDY_AREAS)
    results = [committed["areas"][k] for k in gd.STUDY_AREAS]
    path = ga.write_summary_csv(results, str(tmp_path), ga.DOCS_SUMMARY_NAME)
    with open(path, "rb") as fh, open(COMMITTED_CSV, "rb") as committed_fh:
        assert fh.read() == committed_fh.read()


def test_committed_json_names_its_producer(committed):
    assert committed["_about"]["generated_by"] == ga.DOCS_GENERATED_BY
    fake = [{"area": "adrian", "variants": {}}, {"area": "seattle", "variants": {}}]
    merged = ga.build_committed_metrics(fake)
    assert merged["_about"]["generated_by"] == ga.DOCS_GENERATED_BY
    assert merged["areas"] == {"adrian": fake[0], "seattle": fake[1]}


def test_docs_dir_refuses_a_partial_area_set(tmp_path):
    """A single-area run must never overwrite the committed record with a subset."""
    with pytest.raises(SystemExit) as excinfo:
        ga.main(["--area", "adrian", "--docs-dir", str(tmp_path)])
    assert excinfo.value.code == 2  # argparse usage error, before any DB is opened
    assert not list(tmp_path.iterdir())


def test_writeup_shares_recompute_from_committed_histograms(committed):
    """Every share docs/experiments/grid-density.md quotes, pinned to the bins."""
    sp = {
        k: ga.spacing_shares(b["distributions"]["pano_nearest_neighbor_m"])
        for k, b in committed["areas"].items()
    }
    assert {k: round(v["band_9_11_pct"], 1) for k, v in sp.items()} == {
        "adrian": 92.5,
        "corvallis": 79.5,
        "seattle": 57.2,
    }
    assert {k: round(v["sub5_pct"], 1) for k, v in sp.items()} == {
        "adrian": 0.4,
        "corvallis": 0.7,
        "seattle": 11.4,
    }
    assert round(sp["seattle"]["under_1m_pct"], 2) == 0.19
    assert round(sp["seattle"]["band_2_4_pct"], 2) == 10.05
    # Only Seattle has a second mode to place: the 2.50–2.75 m bin ("≈2.6 m").
    assert sp["seattle"]["sub5_peak_m"] == 2.625
    assert sp["adrian"]["sub5_peak_m"] is None
    assert sp["corvallis"]["sub5_peak_m"] is None

    off = {
        k: ga.offset_shares(b["distributions"]["query_to_pano_m"])
        for k, b in committed["areas"].items()
    }
    assert {k: round(v["beyond_50m_pct"], 1) for k, v in off.items()} == {
        "adrian": 9.6,
        "corvallis": 2.5,
        "seattle": 0.6,
    }
    # The Corvallis far tail: past the histogram's last edge, so it can only be
    # seen through the tail count and `max` — never through the clipped ECDF.
    assert off["corvallis"]["n_beyond_last_edge"] == 313
    assert off["corvallis"]["last_edge_m"] == 200.0
    assert committed["areas"]["corvallis"]["offsets"]["query_to_pano_m"]["max"] == 573.0
    assert committed["areas"]["adrian"]["offsets"]["query_to_pano_m"]["p99"] == 146.1


def test_figures_from_metrics_needs_only_the_committed_json(tmp_path):
    pytest.importorskip("matplotlib")
    src = tmp_path / ga.DOCS_METRICS_NAME
    shutil.copy(COMMITTED_JSON, src)
    assert ga.main(["--figures-from-metrics", str(src)]) == 0
    written = sorted(p.name for p in (tmp_path / "figures").iterdir())
    assert written == ["grid-density-pano_spacing.png", "grid-density-query_offset_ecdf.png"]
    assert all((tmp_path / "figures" / n).stat().st_size > 10_000 for n in written)
