"""
Issue #106 experiment analysis: derive the 20 m / 10 m / 5 m / road-clipped-5 m
variants from each area's single aligned 5 m snapshot and measure where finer
sampling stops paying.

    python scripts/grid_density_analyze.py [--area all] [--out-dir experiments/grid-density]
    python scripts/grid_density_analyze.py --area all --docs-dir docs/experiments
    python scripts/grid_density_analyze.py --figures-from-metrics docs/experiments/grid-density_metrics.json

Per area, writes {area}_metrics.json; across areas, variants_summary.csv,
figures/*.png, and report.md — all under the (gitignored) out-dir. With
--docs-dir it ALSO writes the durable record that is committed beside the
writeup: the merged grid-density_metrics.json (every area, including the
binned distance histograms), grid-density_variants_summary.csv, and the five
figures under docs/experiments/figures/ with a `grid-density-` prefix. That
record is what the writeup's numbers must trace to (CLAUDE.md, "Notes").
--figures-from-metrics redraws only the two distribution figures from that
committed JSON — no DB, no raw CSVs, no geo stack.

Metric semantics deliberately mirror the production pipeline: grid coverage is
analysis.PRESENT_STATUSES over total points; street/pano metrics filter to
official imagery via analysis.is_google_copyright; per-edge street coverage
uses the street_coverage.py sjoin_nearest idiom at --match-dist (default 25 m).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shutil
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ...and the script's own directory, because `grid_density_common` is a sibling
# MODULE, not a package. tests/test_grid_density.py loads this file by path and
# treats it as a library; without this, its `from grid_density_common import ...`
# below resolves only if the importer happened to pre-register that name in
# sys.modules first, so reordering two blocks in the test file would break
# collection with a ModuleNotFoundError that names neither.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geopy  # noqa: E402
from experiment_style import (  # noqa: E402
    CATEGORICAL,
    INK,
    INK_2,
    SURFACE,
    agg_pyplot,
    style_axis,
)
from grid_density_common import (  # noqa: E402
    CLIP_DIST_M_DEFAULT,
    DEFAULT_OUT_DIR,
    FINE_STEP_M,
    STUDY_AREAS,
    attach_indices,
    fine_index_ranges,
    generate_lattice,
    index_ranges_20m,
    lattice_frame,
    quant_key,
    road_clip_mask,
    snapshot_csv_path,
    variant_masks,
)

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.analysis import (  # noqa: E402
    PRESENT_STATUSES,
    calculate_run_stats,
    is_google_copyright,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

# NOTE: streetscape_street_analyzer.download_street_network is imported lazily in
# analyze_area() rather than here — it pulls geopandas and osmnx at import time,
# and --figures-from-metrics exists so the committed figures can be regenerated
# from the committed metrics JSON without them; requiring geopandas to redraw a
# line chart would defeat it.
#
# Be precise about what that buys, because an earlier version of this comment
# claimed the flag needed "no geo stack" at all, and that is false: the imports
# below pull streetscape_metadata_tracker, whose __init__ reaches
# download_mapillary, which imports mapbox_vector_tile -> shapely (and aiohttp)
# at module scope. Measured, not assumed. So the honest claim is "no
# geopandas/osmnx", and the redraw still needs numpy/pandas/matplotlib/shapely.

logger = logging.getLogger(__name__)

# Named rather than inlined into add_argument, so docs_generated_by can tell a
# canonical run from one with the knob moved.
MATCH_DIST_M_DEFAULT = 25.0

VARIANT_ORDER = ["step20", "step10", "step5_road", "step5"]
VARIANT_LABELS = {
    "step20": "20 m grid",
    "step10": "10 m grid",
    "step5": "5 m grid",
    "step5_road": "road-clipped 5 m",
}
# Marginal-returns transitions reported per area: (from, to).
TRANSITIONS = [("step20", "step10"), ("step10", "step5"), ("step20", "step5_road")]
WGS84 = "EPSG:4326"


def _official_present(vdf: pd.DataFrame) -> pd.DataFrame:
    """PRESENT rows carrying official © Google imagery (production semantics)."""
    present = vdf[vdf["status"].isin(PRESENT_STATUSES)]
    return present[is_google_copyright(present["copyright_info"])]


def variant_metrics(vdf: pd.DataFrame, run_date: date) -> dict:
    """Core per-variant stats: pipeline run stats + duplicate/yield rates."""
    stats = calculate_run_stats(vdf, run_date)
    official = _official_present(vdf)
    n_official_panos = int(official["pano_id"].nunique())
    return {
        "points": int(len(vdf)),
        "coverage_rate_pct": stats["coverage_rate_pct"],
        "status_ok": stats["status_ok"],
        "status_no_date": stats["status_no_date"],
        "status_zero_results": stats["status_zero_results"],
        "status_other": stats["status_other"],
        "unique_panos": stats["unique_panos"],
        "unique_google_panos": stats["unique_google_panos"],
        "official_present_rows": int(len(official)),
        "rows_per_official_pano": round(len(official) / n_official_panos, 2)
        if n_official_panos
        else None,
        "official_panos_per_1k_queries": round(1000.0 * n_official_panos / len(vdf), 1),
        "median_pano_age_years": stats["median_pano_age_years"],
    }


def official_pano_sets(variants: dict[str, pd.DataFrame]) -> dict[str, set]:
    return {name: set(_official_present(vdf)["pano_id"]) for name, vdf in variants.items()}


def marginal_metrics(variants: dict[str, pd.DataFrame], metrics: dict[str, dict]) -> list[dict]:
    """New-official-panos-per-extra-query for each step transition."""
    sets = official_pano_sets(variants)
    out = []
    for a, b in TRANSITIONS:
        extra = len(variants[b]) - len(variants[a])
        new = len(sets[b] - sets[a])
        out.append(
            {
                "from": a,
                "to": b,
                "extra_queries": extra,
                "new_official_panos": new,
                "new_panos_per_1k_extra_queries": round(1000.0 * new / extra, 1)
                if extra > 0
                else None,
                "coverage_delta_pct_points": round(
                    metrics[b]["coverage_rate_pct"] - metrics[a]["coverage_rate_pct"], 2
                ),
            }
        )
    return out


def _points_gdf(lats, lons, metric_crs):
    import geopandas as gpd

    return gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs=WGS84).to_crs(metric_crs)


def offset_arrays(df5: pd.DataFrame, metric_crs) -> tuple[np.ndarray, np.ndarray]:
    """
    The two distance arrays behind `offsets` and `distributions`, on the 5 m
    variant in a metric CRS (never the degree-based approximation): query→pano
    offset per official return, and pano↔pano nearest-neighbour spacing per
    unique official pano (`sjoin_nearest`, the street_coverage.py idiom).
    """
    import geopandas as gpd

    official = _official_present(df5).dropna(subset=["pano_lat", "pano_lon"])
    q = _points_gdf(official["query_lat"], official["query_lon"], metric_crs)
    p = _points_gdf(official["pano_lat"], official["pano_lon"], metric_crs)
    offsets = q.distance(p, align=False).to_numpy()

    panos = official.drop_duplicates(subset=["pano_id"])
    pgdf = _points_gdf(panos["pano_lat"], panos["pano_lon"], metric_crs)
    nn = (
        gpd.sjoin_nearest(pgdf, pgdf, how="inner", distance_col="_d", exclusive=True)
        .groupby(level=0)["_d"]
        .min()
        .to_numpy()
    )
    # `nn` carries one distance per unique official pano, which is what lets
    # offset_metrics report len(nn) as `n_unique_official_panos` and the writeup
    # print it as an "n official panos" column. exclusive=True + how="inner"
    # drops a pano with no other pano to pair with, which happens only for a
    # degenerate area holding a single official pano — and there the count would
    # silently read 0 and every share in spacing_shares would divide by it into
    # NaN. Fail loudly instead of publishing that.
    if len(nn) != len(panos):
        raise ValueError(
            f"nearest-neighbour spacing covers {len(nn)} of {len(panos)} unique official "
            "panos; `n_unique_official_panos` would not be a pano count. An area needs at "
            "least two official panos for a spacing distribution to mean anything."
        )
    return offsets, nn


def offset_metrics(offsets: np.ndarray, nn: np.ndarray) -> dict:
    """Percentile summary of `offset_arrays` — the writeup's `offsets` block."""

    def pct(arr, q_):
        return round(float(np.percentile(arr, q_)), 1) if len(arr) else None

    return {
        "n_offset_pairs": int(len(offsets)),
        "query_to_pano_m": {
            "p50": pct(offsets, 50),
            "p90": pct(offsets, 90),
            "p99": pct(offsets, 99),
            "max": round(float(offsets.max()), 1) if len(offsets) else None,
        },
        # One entry per unique official pano — offset_arrays guarantees the
        # 1:1, so this is a pano count and not a neighbour-pair count.
        "n_unique_official_panos": int(len(nn)),
        "pano_nearest_neighbor_m": {
            "p25": pct(nn, 25),
            "p50": pct(nn, 50),
            "p75": pct(nn, 75),
            "p90": pct(nn, 90),
        },
    }


# Fixed histogram frames for the committed `distributions` block. Fixed, not
# data-driven, so two collections (or two areas) bin identically and the
# committed figures/tests can read a bin by position. Values at or beyond the
# last edge are counted in `n_above_last_edge`, never silently dropped — that
# is where the writeup's far offset tail (Corvallis, 2026-07-26) lives.
NN_HIST_EDGES_M = np.linspace(0.0, 30.0, 121)  # 0.25 m bins
OFFSET_HIST_EDGES_M = np.linspace(0.0, 200.0, 101)  # 2 m bins


def distance_histogram(values: np.ndarray, edges: np.ndarray) -> dict:
    """Binned counts over a fixed frame; the tail past the last edge is kept as a count."""
    values = np.asarray(values, dtype=float)
    # Drop NaN before anything counts it. `NaN < edge` and `NaN >= edge` are BOTH
    # False, so a NaN lands in neither `counts` nor `n_above_last_edge` while
    # len() still counts it in `n_total` — quietly breaking the
    # bins + tail == n_total invariant stated below and asserted by
    # tests/test_grid_density.py, and diluting every share in spacing_shares /
    # offset_shares by an amount nothing reports. +-inf is kept: it is a real
    # distance ordering and lands in the tail where it belongs.
    values = values[~np.isnan(values)]
    # np.histogram closes its LAST bin on the right, which would count a value
    # sitting exactly on the last edge both in that bin and in the tail below.
    counts, _ = np.histogram(values[values < edges[-1]], bins=edges)
    return {
        "bin_edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
        "n_above_last_edge": int((values >= edges[-1]).sum()),
        "n_total": int(len(values)),
    }


def distance_distributions(offsets: np.ndarray, nn: np.ndarray) -> dict:
    """
    Compact histograms of the SAME two arrays `offset_metrics` summarizes as
    percentiles, so the ECDF/density figures regenerate from the committed
    JSON alone while the raw 5 m CSVs stay gitignored.
    """
    return {
        "_note": (
            "Compact histograms of the same two distances `offsets` summarizes as "
            "percentiles, computed from the identical arrays in the identical UTM CRS "
            "and committed so the ECDF/density figures regenerate from this file alone "
            "(the raw 5 m CSVs stay gitignored). tests/test_grid_density.py checks that "
            "each `offsets` percentile falls in the histogram bin its cumulative share "
            "implies, and that every share the writeup quotes recomputes from these bins."
        ),
        "pano_nearest_neighbor_m": distance_histogram(nn, NN_HIST_EDGES_M),
        "query_to_pano_m": distance_histogram(offsets, OFFSET_HIST_EDGES_M),
    }


def street_metrics(
    variants: dict[str, pd.DataFrame],
    edges_env,
    metric_crs,
    match_dist_m: float,
) -> dict[str, dict]:
    """Per-variant % of (envelope-restricted) edges/street-length with ≥1 official pano."""
    import geopandas as gpd

    edges_m = edges_env.to_crs(metric_crs)
    total_len = float(edges_m.geometry.length.sum())
    out = {}
    for name, vdf in variants.items():
        official = _official_present(vdf).dropna(subset=["pano_lat", "pano_lon"])
        panos = official.drop_duplicates(subset=["pano_id"])
        if panos.empty:
            out[name] = {
                "edges_covered_pct": 0.0,
                "length_covered_pct": 0.0,
                "covered_edge_ids": [],
            }
            continue
        pgdf = _points_gdf(panos["pano_lat"], panos["pano_lon"], metric_crs)
        joined = gpd.sjoin_nearest(edges_m, pgdf, how="inner", max_distance=match_dist_m)
        covered = edges_m.loc[joined.index.unique()]
        out[name] = {
            "edges_covered_pct": round(100.0 * len(covered) / len(edges_m), 1),
            "length_covered_pct": round(
                100.0 * float(covered.geometry.length.sum()) / total_len, 1
            ),
            "covered_edge_ids": sorted(covered["edge_id"].tolist()),
        }
    return out


def streetwalk_comparison(
    data_dir: str, coverage_filename: str, env_edge_ids: set, street: dict[str, dict]
) -> dict | None:
    """Grid variants vs the road-walk near-census, per-edge, within the envelope."""
    path = os.path.join(data_dir, coverage_filename)
    if not os.path.exists(path):
        logger.warning("Streetwalk artifact missing, skipping comparison: %s", path)
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fc = json.load(fh)
    walk_covered = {
        f["properties"]["edge_id"]
        for f in fc["features"]
        if f["properties"]["covered"] and f["properties"]["edge_id"] in env_edge_ids
    }
    walk_all = {
        f["properties"]["edge_id"]
        for f in fc["features"]
        if f["properties"]["edge_id"] in env_edge_ids
    }
    out = {
        "walk_edges_in_envelope": len(walk_all),
        "walk_covered": len(walk_covered),
        "variants": {},
    }
    for name in VARIANT_ORDER:
        grid_covered = set(street[name]["covered_edge_ids"]) & walk_all
        tp = len(grid_covered & walk_covered)
        out["variants"][name] = {
            "grid_covered": len(grid_covered),
            "recall_vs_walk_pct": round(100.0 * tp / len(walk_covered), 1)
            if walk_covered
            else None,
            "precision_vs_walk_pct": round(100.0 * tp / len(grid_covered), 1)
            if grid_covered
            else None,
        }
    return out


def production_cross_check(df_idx: pd.DataFrame, data_dir: str, csv_filename: str) -> dict:
    """
    Every experiment step20 point must exist (by quantized key) in the production
    run — anything less means the lattice math is wrong. Pano/status agreement
    is informational (imagery drift between collection dates).
    """
    prod = load_city_csv_file(os.path.join(data_dir, csv_filename))
    prod_by_key = {
        quant_key(lat, lon): (pid, status)
        for lat, lon, pid, status in zip(
            prod["query_lat"], prod["query_lon"], prod["pano_id"], prod["status"], strict=True
        )
    }
    sub = df_idx[(df_idx["i"] % 4 == 0) & (df_idx["j"] % 4 == 0)]
    matched = same_pano = same_status = 0
    for row in sub.itertuples(index=False):
        hit = prod_by_key.get(quant_key(row.query_lat, row.query_lon))
        if hit is None:
            continue
        matched += 1
        prod_pid, prod_status = hit
        if pd.isna(row.pano_id) or pd.isna(prod_pid):
            same_pano += int(pd.isna(row.pano_id) and pd.isna(prod_pid))
        elif row.pano_id == prod_pid:
            same_pano += 1
        if row.status == prod_status:
            same_status += 1
    if matched != len(sub):
        # A dated filename means the run was collected from the catalog's
        # frozen geometry — a miss there is a real lattice bug. Legacy
        # baselines (undated, pre-catalog) were collected from whatever
        # center the original geocode produced (Seattle's is ~4 m off the
        # frozen center), so zero overlap is expected and only reported.
        if re.search(r"_\d{4}-\d{2}-\d{2}\.csv\.gz$", csv_filename):
            raise AssertionError(
                f"Production cross-check FAILED: only {matched}/{len(sub)} experiment 20 m "
                f"points found in {csv_filename} — lattice alignment is wrong."
            )
        logger.warning(
            "Legacy baseline %s shares %d/%d lattice keys (pre-catalog origin); "
            "skipping pano/status agreement.",
            csv_filename,
            matched,
            len(sub),
        )
        return {
            "production_csv": csv_filename,
            "points_checked": int(len(sub)),
            "key_match_pct": round(100.0 * matched / len(sub), 1),
            "legacy_baseline_different_origin": True,
            "same_pano_pct": None,
            "same_status_pct": None,
        }
    return {
        "production_csv": csv_filename,
        "points_checked": int(len(sub)),
        "key_match_pct": 100.0,
        "same_pano_pct": round(100.0 * same_pano / matched, 1),
        "same_status_pct": round(100.0 * same_status / matched, 1),
    }


def analyze_area(args, conn, area_key: str) -> dict:
    from shapely.geometry import box

    area = STUDY_AREAS[area_key]
    city = db.resolve_city(conn, area.city_id)
    if city is None:
        raise SystemExit(f"City not in catalog: {area.city_id}")

    csv_path = snapshot_csv_path(args.out_dir, area)
    if not os.path.exists(csv_path):
        hint = (
            " (a .rejected sibling exists — quota-poisoned run)"
            if os.path.exists(csv_path + ".rejected")
            else ""
        )
        raise SystemExit(
            f"Missing experiment snapshot {csv_path}{hint}; run grid_density_collect.py first."
        )
    df = load_city_csv_file(csv_path)

    i20, j20 = index_ranges_20m(city, area)
    i5, j5 = fine_index_ranges(i20, j20, substep=round(city.step_m / FINE_STEP_M))
    origin = geopy.Point(city.center_lat, city.center_lon)
    lattice = lattice_frame(generate_lattice(origin, i5, j5, FINE_STEP_M))
    df_idx = attach_indices(df, lattice)
    run_date = pd.to_datetime(df_idx["query_timestamp"].iloc[0]).date()

    from streetscape_street_analyzer.download_street_network import fetch_street_edges

    edges = fetch_street_edges(city, args.data_dir, conn=None)
    road_mask_lattice = road_clip_mask(lattice, edges, args.clip_dist)
    road_keys = {
        (i, j) for i, j, m in zip(lattice["i"], lattice["j"], road_mask_lattice, strict=True) if m
    }

    masks = variant_masks(df_idx["i"], df_idx["j"])
    assert not (masks["step20"] & ~masks["step10"]).any()
    assert int(masks["step20"].sum()) == len(i20) * len(j20)
    row_road = np.array(
        [(i, j) in road_keys for i, j in zip(df_idx["i"], df_idx["j"], strict=True)], dtype=bool
    )
    variants = {
        "step20": df_idx[masks["step20"]],
        "step10": df_idx[masks["step10"]],
        "step5": df_idx,
        "step5_road": df_idx[row_road],
    }

    metrics = {name: variant_metrics(vdf, run_date) for name, vdf in variants.items()}
    marginal = marginal_metrics(variants, metrics)

    metric_crs = edges.estimate_utm_crs()
    offsets_m, nn_m = offset_arrays(variants["step5"], metric_crs)
    offsets = offset_metrics(offsets_m, nn_m)
    distributions = distance_distributions(offsets_m, nn_m)

    # Envelope: edges intersecting the lattice bbox (+ match-dist margin).
    lat_pts = _points_gdf(lattice["lat"], lattice["lon"], metric_crs)
    minx, miny, maxx, maxy = lat_pts.total_bounds
    env = box(
        minx - args.match_dist,
        miny - args.match_dist,
        maxx + args.match_dist,
        maxy + args.match_dist,
    )
    edges_m_all = edges.to_crs(metric_crs)
    edges_env = edges[edges_m_all.intersects(env)]
    street = street_metrics(variants, edges_env, metric_crs, args.match_dist)

    walk_row = conn.execute(
        "SELECT coverage_filename FROM street_walks WHERE city_id = ? AND provider = 'gsv' "
        "ORDER BY run_date DESC LIMIT 1",
        (city.city_id,),
    ).fetchone()
    walk_cmp = (
        streetwalk_comparison(
            args.data_dir, walk_row["coverage_filename"], set(edges_env["edge_id"]), street
        )
        if walk_row
        else None
    )

    run_row = conn.execute(
        "SELECT csv_filename, run_date FROM runs WHERE city_id = ? AND provider = 'gsv' "
        "ORDER BY run_date DESC LIMIT 1",
        (city.city_id,),
    ).fetchone()
    cross = (
        production_cross_check(df_idx, args.data_dir, run_row["csv_filename"]) if run_row else None
    )
    if cross:
        cross["production_run_date"] = run_row["run_date"]

    result = {
        "area": area.key,
        "label": area.label,
        "city_id": city.city_id,
        "collection_date": run_date.isoformat(),
        "clip_dist_m": args.clip_dist,
        "match_dist_m": args.match_dist,
        "variants": metrics,
        "marginal": marginal,
        "offsets": offsets,
        "street": {
            name: {k: v for k, v in m.items() if k != "covered_edge_ids"}
            for name, m in street.items()
        },
        "street_edges_in_envelope": int(len(edges_env)),
        "streetwalk_comparison": walk_cmp,
        "production_cross_check": cross,
        "distributions": distributions,
    }
    out_json = os.path.join(args.out_dir, f"{area.key}_metrics.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    logger.info("Wrote %s", out_json)
    return result


# ── Figures + report ────────────────────────────────────────────────────────

# Fixed categorical order (blue, orange, aqua) for the three areas. The palette
# and the axis treatment are shared with the other experiment writeups; see
# scripts/experiment_style.py.
AREA_COLORS = dict(zip(("adrian", "corvallis", "seattle"), CATEGORICAL, strict=True))


def _style_axis(ax):
    # y-grid on: the bar and ECDF panels here are read against horizontal rules.
    style_axis(ax, ygrid=True)


def make_figures(results: list[dict], fig_dir: str, prefix: str = "") -> list[str]:
    plt = agg_pyplot()

    os.makedirs(fig_dir, exist_ok=True)
    written = []

    # 1. Marginal-returns curve: unique official panos vs queries (log-x).
    fig, ax = plt.subplots(figsize=(7, 4.5), facecolor=SURFACE)
    _style_axis(ax)
    for r in results:
        color = AREA_COLORS[r["area"]]
        # Normalize to the area's 5 m pano total: areas differ by orders of
        # magnitude in absolute counts, and the curve SHAPE is the message.
        full = r["variants"]["step5"]["unique_google_panos"]
        pts = [
            (r["variants"][v]["points"], 100.0 * r["variants"][v]["unique_google_panos"] / full)
            for v in ("step20", "step10", "step5")
        ]
        xs_, ys_ = zip(*pts, strict=True)
        ax.plot(xs_, ys_, "-o", color=color, linewidth=2, markersize=6, label=r["area"])
        road = r["variants"]["step5_road"]
        ax.plot(
            road["points"],
            100.0 * road["unique_google_panos"] / full,
            "D",
            color=color,
            markersize=7,
            markerfacecolor="none",
            markeredgewidth=2,
        )
    ax.set_xscale("log")
    ax.set_xlabel("API queries (log scale)", color=INK_2, fontsize=10)
    ax.set_ylabel("Official panos found (% of 5 m total)", color=INK_2, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="lower right")
    ax.set_title(
        "Marginal pano discovery: 20 m → 10 m → 5 m (◇ = road-clipped 5 m)",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, f"{prefix}marginal_panos_vs_queries.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 2. Grid coverage % by variant (grouped bars, direct labels).
    fig, ax = plt.subplots(figsize=(7, 4), facecolor=SURFACE)
    _style_axis(ax)
    width = 0.26
    xs = np.arange(len(VARIANT_ORDER))
    for k, r in enumerate(results):
        vals = [r["variants"][v]["coverage_rate_pct"] for v in VARIANT_ORDER]
        bars = ax.bar(
            xs + (k - 1) * width, vals, width * 0.92, color=AREA_COLORS[r["area"]], label=r["area"]
        )
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(
                f"{v:.0f}",
                (b.get_x() + b.get_width() / 2, v),
                textcoords="offset points",
                xytext=(0, 2),
                ha="center",
                fontsize=8,
                color=INK_2,
            )
    ax.set_xticks(xs, [VARIANT_LABELS[v] for v in VARIANT_ORDER])
    ax.set_ylabel("Grid coverage (% of sampled points)", color=INK_2, fontsize=10)
    ax.set_title("Coverage rate by sampling variant", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2)
    fig.tight_layout()
    p = os.path.join(fig_dir, f"{prefix}coverage_by_variant.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 3. Street-length coverage by variant, with streetwalk baseline.
    fig, ax = plt.subplots(figsize=(7, 4), facecolor=SURFACE)
    _style_axis(ax)
    for k, r in enumerate(results):
        vals = [r["street"][v]["length_covered_pct"] for v in VARIANT_ORDER]
        bars = ax.bar(
            xs + (k - 1) * width, vals, width * 0.92, color=AREA_COLORS[r["area"]], label=r["area"]
        )
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(
                f"{v:.0f}",
                (b.get_x() + b.get_width() / 2, v),
                textcoords="offset points",
                xytext=(0, 2),
                ha="center",
                fontsize=8,
                color=INK_2,
            )
    ax.set_xticks(xs, [VARIANT_LABELS[v] for v in VARIANT_ORDER])
    ax.set_ylabel("Street length with ≥1 pano within 25 m (%)", color=INK_2, fontsize=10)
    ax.set_title("Street-network coverage by sampling variant", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2)
    fig.tight_layout()
    p = os.path.join(fig_dir, f"{prefix}street_coverage_by_variant.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)
    return written


def write_summary_csv(
    results: list[dict], out_dir: str, filename: str = "variants_summary.csv"
) -> str:
    rows = []
    for r in results:
        for v in VARIANT_ORDER:
            m = r["variants"][v]
            rows.append(
                {
                    "area": r["area"],
                    "variant": v,
                    "points": m["points"],
                    "coverage_rate_pct": round(m["coverage_rate_pct"], 2),
                    "unique_google_panos": m["unique_google_panos"],
                    "rows_per_official_pano": m["rows_per_official_pano"],
                    "official_panos_per_1k_queries": m["official_panos_per_1k_queries"],
                    "street_length_covered_pct": r["street"][v]["length_covered_pct"],
                }
            )
    path = os.path.join(out_dir, filename)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def write_report(results: list[dict], out_dir: str) -> str:
    lines = [
        "# Grid density cost/benefit (issue #106)",
        "",
        f"Generated by scripts/grid_density_analyze.py. Areas: "
        f"{', '.join(r['label'] for r in results)}.",
        "",
        "One aligned 5 m collection per area; 20 m / 10 m / road-clipped variants "
        "derived offline (bit-identical subsets). Official-pano semantics per "
        "analysis.is_google_copyright; street coverage per street_coverage.py's "
        "25 m nearest-join.",
        "",
    ]
    for r in results:
        lines += [f"## {r['label']}", ""]
        lines += [
            "| variant | queries | coverage % | unique official panos | rows/pano | panos per 1k queries | street-length covered % |",
            "|---|---|---|---|---|---|---|",
        ]
        for v in VARIANT_ORDER:
            m = r["variants"][v]
            lines.append(
                f"| {VARIANT_LABELS[v]} | {m['points']:,} | {m['coverage_rate_pct']:.1f} | "
                f"{m['unique_google_panos']:,} | {m['rows_per_official_pano']} | "
                f"{m['official_panos_per_1k_queries']} | {r['street'][v]['length_covered_pct']} |"
            )
        lines += ["", "**Marginal returns:**", ""]
        for t in r["marginal"]:
            lines.append(
                f"- {VARIANT_LABELS[t['from']]} → {VARIANT_LABELS[t['to']]}: "
                f"{t['extra_queries']:+,} queries → {t['new_official_panos']:,} new official panos "
                f"({t['new_panos_per_1k_extra_queries']} per 1k extra queries); "
                f"coverage {t['coverage_delta_pct_points']:+.1f} pp"
            )
        o = r["offsets"]
        lines += [
            "",
            f"Query→pano offset (5 m variant): p50 {o['query_to_pano_m']['p50']} m, "
            f"p90 {o['query_to_pano_m']['p90']} m. Official pano nearest-neighbor spacing: "
            f"p25 {o['pano_nearest_neighbor_m']['p25']} m, p50 {o['pano_nearest_neighbor_m']['p50']} m, "
            f"p75 {o['pano_nearest_neighbor_m']['p75']} m (n={o['n_unique_official_panos']:,}).",
        ]
        if r["streetwalk_comparison"]:
            w = r["streetwalk_comparison"]
            lines += ["", "**Vs. road-walk near-census (per-edge, envelope-restricted):**", ""]
            for v in VARIANT_ORDER:
                wv = w["variants"][v]
                lines.append(
                    f"- {VARIANT_LABELS[v]}: recall {wv['recall_vs_walk_pct']}%, "
                    f"precision {wv['precision_vs_walk_pct']}% "
                    f"(walk covered {w['walk_covered']}/{w['walk_edges_in_envelope']} edges)"
                )
        if r["production_cross_check"]:
            c = r["production_cross_check"]
            if c.get("legacy_baseline_different_origin"):
                agreement = "pre-catalog origin, no shared lattice — alignment check N/A"
            else:
                agreement = (
                    f"key match {c['key_match_pct']}%, same pano {c['same_pano_pct']}%, "
                    f"same status {c['same_status_pct']}%"
                )
            lines += [
                "",
                f"Production cross-check vs {c['production_csv']} "
                f"({c['production_run_date']}): {agreement}.",
            ]
        lines.append("")
    lines += [
        "## Figures",
        "",
        "![marginal](figures/marginal_panos_vs_queries.png)",
        "![coverage](figures/coverage_by_variant.png)",
        "![street](figures/street_coverage_by_variant.png)",
        "",
        "## Caveats",
        "",
        "- The derived 10 m variant can be ≤1 row/col smaller than a native "
        "int(dim/10) production grid (index-subset construction).",
        "- Seattle's only production run (2024-12-19) is a pre-catalog legacy "
        "baseline collected from a different grid origin (~4 m off the frozen "
        "center), so it shares no lattice keys with the experiment (or with future "
        "frozen-geometry runs; diff.py handles this via grid_aligned=False).",
        "- Tile envelopes cut edges at the boundary; absolute street-coverage "
        "percentages are envelope-relative, variant-to-variant deltas are not affected.",
        "- Grid coverage % is not comparable across variants for the road-clipped "
        "case by design: its denominator contains only near-street points.",
        "",
    ]
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def spacing_shares(hist: dict) -> dict:
    """
    The shares the writeup quotes from a `pano_nearest_neighbor_m` histogram:
    the +-1 m band around 10 m (the evidence the interval is regulated — a
    bimodal distribution can have the same median), the sub-5 m share (a
    neighbour on a *different* roadway), and where that sub-5 m mass peaks.
    `sub5_peak_m` is None when the sub-5 m mass has no bin above 1% of the
    total, i.e. there is no second mode to place.
    """
    edges = np.asarray(hist["bin_edges"], dtype=float)
    centers = (edges[:-1] + edges[1:]) / 2.0
    share = 100.0 * np.asarray(hist["counts"], dtype=float) / hist["n_total"]
    sub5_mask = centers < 5.0
    sub5_share = share[sub5_mask]
    peak_idx = int(np.argmax(sub5_share)) if sub5_share.size else None
    has_mode = peak_idx is not None and sub5_share[peak_idx] >= 1.0
    return {
        "band_9_11_pct": float(share[(centers >= 9.0) & (centers <= 11.0)].sum()),
        "sub5_pct": float(sub5_share.sum()),
        "under_1m_pct": float(share[centers < 1.0].sum()),
        "band_2_4_pct": float(share[(centers >= 2.0) & (centers < 4.0)].sum()),
        "sub5_peak_m": float(centers[sub5_mask][peak_idx]) if has_mode else None,
        "sub5_peak_pct": float(sub5_share[peak_idx]) if has_mode else None,
    }


def offset_shares(hist: dict) -> dict:
    """Beyond-50 m share (Google's documented default radius) and the far tail."""
    xs_, ys_ = _ecdf(hist)
    return {
        "beyond_50m_pct": float(100.0 - np.interp(50.0, xs_, ys_)),
        "n_beyond_last_edge": int(hist["n_above_last_edge"]),
        "last_edge_m": float(hist["bin_edges"][-1]),
    }


def _ecdf(hist: dict) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative % at each bin's right edge, from a committed histogram block."""
    counts = np.asarray(hist["counts"], dtype=float)
    edges = np.asarray(hist["bin_edges"], dtype=float)
    return edges[1:], 100.0 * np.cumsum(counts) / hist["n_total"]


def make_distribution_figures(areas: dict, fig_dir: str, prefix: str) -> list[str]:
    """
    The two distance-distribution figures, plotted from the committed
    `grid-density_metrics.json` histograms rather than the (gitignored) raw
    CSVs — so they regenerate from what is in git.

    Deliberately two different forms, because the questions differ: spacing asks
    about SHAPE (is the interval regulated?), so it gets a density curve where
    the ~10 m spike is the message; offset asks about THRESHOLD EXCEEDANCE (how
    much lands beyond Google's documented radius?), so it gets an ECDF where a
    reference line and the share past it can be read directly.
    """
    plt = agg_pyplot()

    # Plot every area the record contains, or refuse. Filtering through
    # AREA_COLORS alone was silent in the direction that matters: adding a fourth
    # study area would write a JSON and CSV containing it beside two committed
    # figures that omit it, exit 0. (make_figures raises KeyError on the same
    # input, so silence here also made the two figure paths disagree.)
    unknown = sorted(set(areas) - set(AREA_COLORS))
    if unknown:
        raise ValueError(
            f"no plot colour for area(s) {unknown}; add them to AREA_COLORS rather than "
            "publishing figures that omit an area the JSON and CSV beside them contain"
        )
    if not areas:
        raise ValueError(f"nothing to plot: expected areas from {sorted(AREA_COLORS)}")
    os.makedirs(fig_dir, exist_ok=True)
    written = []
    ordered = [(k, areas[k]) for k in AREA_COLORS if k in areas]

    # 1. Pano-to-pano spacing density. Share per bin, not counts: the areas
    # differ ~18x in pano count and the SHAPE is the finding.
    fig, ax = plt.subplots(figsize=(7, 4.5), facecolor=SURFACE)
    _style_axis(ax)
    shares = {}
    for key, blk in ordered:
        h = blk["distributions"]["pano_nearest_neighbor_m"]
        edges = np.asarray(h["bin_edges"], dtype=float)
        centers = (edges[:-1] + edges[1:]) / 2.0
        share = 100.0 * np.asarray(h["counts"], dtype=float) / h["n_total"]
        # The share landing in a +-1 m band around 10 m is the actual evidence
        # that the interval is regulated; the median alone cannot show it,
        # because a bimodal distribution can have the same median.
        shares[key] = spacing_shares(h)
        band = shares[key]["band_9_11_pct"]
        ax.plot(
            centers,
            share,
            drawstyle="steps-mid",
            color=AREA_COLORS[key],
            linewidth=2,
            label=f"{blk['label'].split('(')[0].strip()} (n={h['n_total']:,})",
        )
        # Direct label at each peak — identity is never color-alone. Peaks sit
        # at clearly different heights, so a plain right-offset cannot collide.
        pk = int(np.argmax(share))
        ax.annotate(
            f"{band:.1f}% within 9–11 m",
            xy=(centers[pk], share[pk]),
            xytext=(9, -3),
            textcoords="offset points",
            color=AREA_COLORS[key],
            fontsize=9,
            fontweight="bold",
        )
    # No text label on this line: the x-axis tick sits on 10 and every direct
    # label already names the 9-11 m band, so a "10 m" tag only adds collisions.
    ax.axvline(10.0, color=INK_2, linewidth=1, linestyle="--", alpha=0.55)
    # The secondary sub-5 m mode is a finding, not noise: it is where nearest
    # neighbour stops measuring the along-track capture interval and starts
    # measuring the distance to a *different roadway*. Which roadway (a second
    # pass, a bridge deck over the road beneath, an intersection) is deliberately
    # NOT claimed here — see the writeup's mechanism table; the label says only
    # what the distance data supports. Where the mode sits is READ from the
    # histogram, not hard-coded, so a re-collection or another city relabels
    # itself; the annotation is drawn only if some area actually has one.
    worst = max(shares, key=lambda k: shares[k]["sub5_pct"])
    peak_m = shares[worst]["sub5_peak_m"]
    if peak_m is not None:
        ax.annotate(
            f"second mode ≈{peak_m:.1f} m\n{worst.title()}: "
            f"{shares[worst]['sub5_pct']:.1f}% under 5 m\n(neighbour on another roadway)",
            xy=(peak_m, shares[worst]["sub5_peak_pct"]),
            xytext=(0.6, 17),
            textcoords="data",
            color=INK_2,
            fontsize=8.5,
            arrowprops={"arrowstyle": "-", "color": INK_2, "alpha": 0.5, "linewidth": 1},
        )
    ax.set_xlim(0, 20)
    ax.set_xlabel("Distance to nearest other official pano (m)", color=INK_2, fontsize=10)
    ax.set_ylabel("Share of official panos (% per 0.25 m bin)", color=INK_2, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left")
    ax.set_title(
        "Official GSV panos sit on a regulated ~10 m interval",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, f"{prefix}pano_spacing.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 2. Query-to-pano offset ECDF, against the documented 50 m default radius.
    fig, ax = plt.subplots(figsize=(7, 4.5), facecolor=SURFACE)
    _style_axis(ax)
    for i, (key, blk) in enumerate(ordered):
        h = blk["distributions"]["query_to_pano_m"]
        xs_, ys_ = _ecdf(h)
        ax.plot(
            xs_,
            ys_,
            color=AREA_COLORS[key],
            linewidth=2,
            label=blk["label"].split("(")[0].strip(),
        )
        # All three curves are near-saturated at x=50, so labels anchored to the
        # curve would overlap. Stack them in the empty mid-right of the plot and
        # lead back to the crossing point.
        # Through offset_shares, not a second inline copy of its expression, so
        # the plotted number IS the tested one: tests/test_grid_density.py pins
        # that helper to the writeup's 9.6 / 2.5 / 0.6. The spacing figure above
        # routes through spacing_shares for the same reason.
        beyond50 = offset_shares(h)["beyond_50m_pct"]
        at50 = 100.0 - beyond50
        ax.annotate(
            f"{beyond50:.1f}% beyond 50 m",
            xy=(50.0, at50),
            xytext=(74.0, 74.0 - i * 9.0),
            textcoords="data",
            color=AREA_COLORS[key],
            fontsize=9,
            fontweight="bold",
            arrowprops={
                "arrowstyle": "-",
                "color": AREA_COLORS[key],
                "alpha": 0.45,
                "linewidth": 1,
            },
        )
    ax.axvline(50.0, color=INK_2, linewidth=1, linestyle="--", alpha=0.55)
    ax.annotate(
        "Google's documented\ndefault radius (50 m)",
        xy=(50.0, 8),
        xytext=(-96, 0),
        textcoords="offset points",
        color=INK_2,
        fontsize=9,
    )
    ax.set_xlim(0, 160)
    ax.set_ylim(0, 101)
    ax.set_xlabel("Distance from query point to returned pano (m)", color=INK_2, fontsize=10)
    ax.set_ylabel("Cumulative share of official returns (%)", color=INK_2, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="lower right")
    ax.set_title(
        "Returns exceed Google's documented 50 m default radius (we set none)",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, f"{prefix}query_offset_ecdf.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)
    return written


# ── The committed record (docs/experiments) ─────────────────────────────────

DOCS_METRICS_NAME = "grid-density_metrics.json"
DOCS_SUMMARY_NAME = "grid-density_variants_summary.csv"
DOCS_FIGURE_PREFIX = "grid-density-"
DOCS_GENERATED_BY = "scripts/grid_density_analyze.py --area all --docs-dir docs/experiments"
# Staging directory for the atomic promote in write_docs_record; per-pid so two
# concurrent writers cannot promote each other's half-rendered artifacts.
DOCS_STAGING_PREFIX = ".grid-density-staging-"


def docs_generated_by(docs_dir: str, clip_dist: float, match_dist: float) -> str:
    """
    The command that actually produced the record, for `_about.generated_by`.

    Stamping a fixed constant would let `--docs-dir /tmp/scratch --clip-dist 30`
    write a file claiming it came from the canonical invocation — a provenance
    claim that is true of no run in particular, which is exactly what CLAUDE.md's
    "Notes" rule (the JSON must be produced by committed code) asks this field to
    rule out. Defaults are omitted, so the canonical run renders exactly
    DOCS_GENERATED_BY and the committed file is unchanged by this.
    """
    cmd = f"scripts/grid_density_analyze.py --area all --docs-dir {docs_dir}"
    if clip_dist != CLIP_DIST_M_DEFAULT:
        cmd += f" --clip-dist {clip_dist:g}"
    if match_dist != MATCH_DIST_M_DEFAULT:
        cmd += f" --match-dist {match_dist:g}"
    return cmd


def build_committed_metrics(results: list[dict], generated_by: str = DOCS_GENERATED_BY) -> dict:
    """
    Merge the per-area results into the single JSON committed beside the
    writeup. Pure assembly: every number is the per-area result verbatim.
    """
    return {
        "_about": {
            "experiment": "grid-density",
            "writeup": "docs/experiments/grid-density.md",
            "issue": 106,
            "generated_by": generated_by,
            "note": (
                "Committed metrics for the grid-density experiment (issue #106). The raw 5 m "
                "collection CSVs stay in the gitignored /experiments/grid-density/ and are "
                "regenerable via scripts/grid_density_collect.py; this file is the durable "
                "record of the derived numbers the writeup cites. Per-area distributions[] "
                "carries binned histograms so the figures are reproducible without the raw "
                "collection."
            ),
        },
        "areas": {r["area"]: r for r in results},
    }


def write_docs_record(
    results: list[dict], docs_dir: str, generated_by: str = DOCS_GENERATED_BY
) -> list[str]:
    """
    Write the durable record: merged metrics JSON, summary CSV, and the five
    figures (prefixed, under docs_dir/figures). This is the ONLY producer of
    those files — the writeup's numbers must trace to what it writes.

    Everything is rendered into a staging directory and promoted with os.replace
    only once all seven artifacts exist. Writing the JSON and CSV first, as this
    used to, meant a figure failing to render (missing backend, stale font cache,
    an area with no colour) left new numbers on disk beside stale PNGs, with
    `git status` flagging only the two files that changed — easy to commit, and
    the one thing a "durable record" must not be. Same verify-then-promote
    posture as catalog_backup.
    """
    os.makedirs(os.path.join(docs_dir, "figures"), exist_ok=True)
    staging = os.path.join(docs_dir, f"{DOCS_STAGING_PREFIX}{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(os.path.join(staging, "figures"))
    try:
        metrics = build_committed_metrics(results, generated_by)
        staged = [os.path.join(staging, DOCS_METRICS_NAME)]
        with open(staged[0], "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2)
            fh.write("\n")
        staged.append(write_summary_csv(results, staging, DOCS_SUMMARY_NAME))
        fig_dir = os.path.join(staging, "figures")
        staged += make_figures(results, fig_dir, prefix=DOCS_FIGURE_PREFIX)
        staged += make_distribution_figures(metrics["areas"], fig_dir, DOCS_FIGURE_PREFIX)
        written = []
        for path in staged:
            dst = os.path.join(docs_dir, os.path.relpath(path, staging))
            os.replace(path, dst)
            written.append(dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return written


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #106 grid-density experiment analysis.")
    parser.add_argument("--area", default="all", choices=[*STUDY_AREAS, "all"])
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument("--clip-dist", type=float, default=CLIP_DIST_M_DEFAULT)
    parser.add_argument("--match-dist", type=float, default=MATCH_DIST_M_DEFAULT)
    parser.add_argument("--no-figures", action="store_true")
    parser.add_argument(
        "--docs-dir",
        metavar="DIR",
        help=(
            f"Also write the committed record beside the writeup: {DOCS_METRICS_NAME} "
            f"(all areas merged, with the binned distributions), {DOCS_SUMMARY_NAME}, and "
            f"the five figures under DIR/figures with a '{DOCS_FIGURE_PREFIX}' prefix. "
            "Requires --area all, so a partial run can never overwrite the record with a "
            "subset of areas."
        ),
    )
    parser.add_argument(
        "--figures-from-metrics",
        metavar="PATH",
        help=(
            "Regenerate only the distance-distribution figures from a merged "
            f"{DOCS_METRICS_NAME} and exit. Needs no DB, no raw CSVs and no "
            "geopandas/osmnx — the point is that these figures survive without the "
            "(gitignored) raw collection (it does still need numpy/pandas/matplotlib, "
            "and shapely arrives transitively). Requires every study area, for the "
            "same reason --docs-dir requires --area all: it overwrites the committed "
            "record's figures in place."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)
    if args.docs_dir and args.area != "all":
        parser.error("--docs-dir requires --area all (the record covers every area)")
    if args.docs_dir and args.no_figures:
        # Refusing beats honouring it: the record's figures are drawn from the
        # numbers beside them, so a numbers-only refresh is precisely the stale
        # pairing write_docs_record stages its artifacts to avoid. Silently
        # ignoring the flag, which is what used to happen, is worse than both.
        parser.error(
            "--no-figures cannot be combined with --docs-dir: the record's figures must "
            "be regenerated with its numbers, or they no longer describe it"
        )
    logging.basicConfig(
        level=getattr(logging, args.log_level), format="%(asctime)s - %(levelname)s - %(message)s"
    )

    if args.figures_from_metrics:
        with open(args.figures_from_metrics, encoding="utf-8") as fh:
            metrics = json.load(fh)
        areas = metrics.get("areas")
        if not isinstance(areas, dict):
            parser.error(
                f"{args.figures_from_metrics} has no 'areas' block — this flag wants the "
                f"merged {DOCS_METRICS_NAME}, not a per-area {{area}}_metrics.json"
            )
        missing = sorted(set(STUDY_AREAS) - set(areas))
        if missing:
            # This path writes the SAME committed filenames --docs-dir does, into
            # figures/ beside the file it read, so without this guard a partial or
            # hand-edited JSON silently republishes the record's figures with an
            # area missing and exits 0 — the --area all guard, one door down.
            parser.error(
                f"{args.figures_from_metrics} is missing area(s) {missing}; refusing to "
                "redraw, because these figures overwrite the committed record in place"
            )
        fig_dir = os.path.join(os.path.dirname(args.figures_from_metrics), "figures")
        for p in make_distribution_figures(areas, fig_dir, DOCS_FIGURE_PREFIX):
            print(f"Figure: {p}")
        return 0

    conn = db.connect(db.get_default_db_path(args.data_dir))
    try:
        area_keys = list(STUDY_AREAS) if args.area == "all" else [args.area]
        results = [analyze_area(args, conn, k) for k in area_keys]
    finally:
        conn.close()

    print(f"Summary: {write_summary_csv(results, args.out_dir)}")
    if not args.no_figures and len(results) > 0:
        for p in make_figures(results, os.path.join(args.out_dir, "figures")):
            print(f"Figure: {p}")
    print(f"Report: {write_report(results, args.out_dir)}")
    if args.docs_dir:
        stamp = docs_generated_by(args.docs_dir, args.clip_dist, args.match_dist)
        for p in write_docs_record(results, args.docs_dir, stamp):
            print(f"Docs: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
