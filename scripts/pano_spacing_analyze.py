"""
Capture-spacing analysis: how finely do GSV and Mapillary sample a street?

    python scripts/pano_spacing_analyze.py [--city all] [--in-dir experiments/pano-spacing]

Reads Mapillary run CSVs (the census artifacts, which carry one row per 360 pano)
and measures the distance between consecutive capture points. Writes
{city}_metrics.json per city and a combined pano-spacing_metrics.json, all under
the (gitignored) --out-dir; the committed copy lives beside the writeup.

WHY THE ESTIMATOR IS WITHIN-SEQUENCE, AND WHY THAT IS THE WHOLE POINT
--------------------------------------------------------------------
The obvious estimator — nearest-neighbour over the pooled pano set — answers the
wrong question for Mapillary, and badly. Many contributors drive the same popular
streets independently, so a pooled nearest neighbour is usually an image from
someone else's drive rather than the next capture along this one. Pooled NN would
therefore measure *how many people drove this road* and return a spuriously small
number that reads as "Mapillary samples finer than GSV".

#106 measured exactly this failure on GSV, where it is far milder: Seattle's
official panos show a second mode at ~2.6 m carrying 11.4% of panos, which is a
neighbour on a *different roadway*, not the along-track capture interval (see
docs/experiments/grid-density.md, and issue #223 for which roadway).

Mapillary publishes `sequence_id` — one capture drive — so the correction is
available here. GSV publishes no run identifier at all, so it is not available
there. That inverts the expected reliability of the two providers, and it is the
single most important methodological point in the writeup. We therefore compute
BOTH and report both: the pooled figure is published not as a metric but as the
measured size of the contamination it would introduce.

Both are nearest-neighbour rather than ordered along-track gaps: the published
CSV carries capture_date at day resolution only, so consecutive images within a
sequence cannot be ordered in time. For a linear track NN is min(gap_before,
gap_after) and therefore a mild UNDERestimate of the mean gap. Ordered gaps need
`captured_at` in milliseconds, which the tile census holds in memory but does not
write to the CSV.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys

import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from streetscape_metadata_tracker import naming  # noqa: E402
from streetscape_metadata_tracker.analysis import PRESENT_STATUSES  # noqa: E402
from streetscape_metadata_tracker.config import MAPILLARY_METADATA_DTYPES  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_style import (  # noqa: E402
    CATEGORICAL,
    GRID,
    INK,
    INK_2,
    SURFACE,
    agg_pyplot,
    style_axis,
)

logger = logging.getLogger(__name__)

# Anchored to the checkout rather than the process CWD. The documented command
# rewrites the committed record, and a CWD-relative default fails LATE — after
# the JSON is written but before the figures are — which leaves new numbers
# beside stale figures. grid_density_common.DEFAULT_OUT_DIR is anchored for the
# same reason.
DEFAULT_IO_DIR = os.path.join(_REPO_ROOT, "experiments", "pano-spacing")
WGS84 = "EPSG:4326"

# The committed record covers exactly these cities. --docs-dir refuses a partial
# set: the writeup's claims are cross-city, so a record missing one would
# silently under-report the spread that is the whole finding.
STUDY_CITIES = (
    "budapest--budapest--hungary",
    "hamtramck--michigan--united-states",
    "san-francisco--california--united-states",
)

# Only the columns something here actually reads — a census CSV is hundreds of
# MB, so an unused column is parsed for every one of ~872k rows for nothing.
# pano_lat/lon position the imagery; query_lat/lon are the grid points the
# coverage denominator counts; status/is_pano select 360 panos; organization_id
# and on_foot are the capture-setup strata. pano_id, capture_date and
# quality_score were dropped when it turned out nothing referenced them.
USECOLS = [
    "query_lat",
    "query_lon",
    "pano_lat",
    "pano_lon",
    "status",
    "organization_id",
    "sequence_id",
    "is_pano",
    "on_foot",
]

# Bin width must stay ABOVE the measurement floor. z14 tile coordinates quantize
# position to ~0.40-0.47 m at these latitudes (see quantization_m), so distances
# between decoded points take discrete values; binning finer than that resolves
# the tile lattice rather than the imagery and renders as a comb of alternating
# full and empty bins, which reads as spurious multi-modality. 0.5 m clears the
# floor for every city here. Re-check quantization_m before analysing a city
# nearer the equator, where a tile unit is larger (~0.6 m).
SPACING_BINS = np.arange(0.0, 60.0001, 0.5)

# A sequence needs at least this many images before its spacing means anything.
MIN_SEQUENCE_LEN = 3


def quantization_m(lat_deg: float, zoom: int = 14, extent: int = 4096) -> float:
    """
    Ground size of one tile-coordinate unit — the measurement floor.

    Mapillary vector tiles carry integer coordinates in a 0..extent grid over the
    tile, so a decoded position is snapped to this spacing. It is small next to a
    3-20 m capture interval but it is not zero, and it is why the histogram is
    binned no finer than 0.5 m (see SPACING_BINS).
    """
    earth_circumference_m = 40075016.686
    tile_width_m = earth_circumference_m * np.cos(np.radians(lat_deg)) / (2**zoom)
    return float(tile_width_m / extent)


def city_id_from_run(path: str) -> str:
    """
    The canonical city_id, via the repo's single source of truth for filenames.

    naming.parse_filename also rejects streetwalk / diff / history artifacts,
    which a `*_mapillary_*.csv.gz` glob happily matches — see is_mapillary_run.
    """
    return naming.parse_filename(path).slug


def is_mapillary_run(path: str) -> bool:
    """
    True iff this is a Mapillary GRID RUN csv, not some other Mapillary artifact.

    The Replicating block tells the reader to rsync `CITY_*_mapillary_DATE.csv.gz`;
    a wildcard date also matches `..._streetwalk_sp15_DATE.csv.gz` and
    `CITY_diff_mapillary_A_to_B.csv.gz`. Neither carries sequence_id, so without
    this they reach load_census and fail with a vintage diagnosis that is simply
    wrong for a 2026-08 file. parse_filename rejects them by design.
    """
    try:
        return naming.parse_filename(path).provider == "mapillary"
    except ValueError:
        return False


def load_census(path: str) -> pd.DataFrame:
    """
    Load one Mapillary run CSV — every row — with the repo's own column types.

    Types come from config.MAPILLARY_METADATA_DTYPES rather than pandas'
    inference: Mapillary ids are 16-19-digit integers, which infer as float64
    and silently round above 2**53. pandas ignores dtype keys absent from a
    file, so this keeps working as the schema grows.
    """
    df = pd.read_csv(
        path,
        usecols=lambda c: c in USECOLS,
        dtype={k: v for k, v in MAPILLARY_METADATA_DTYPES.items() if k in USECOLS},
    )
    missing = [c for c in ("sequence_id", "is_pano") if c not in df.columns]
    if missing:
        # Pre-2026-07-23 vintage: the extras columns do not exist, so the
        # within-sequence estimator is impossible and the pooled one would be
        # misleading. Refuse rather than silently publishing the wrong number.
        raise SystemExit(
            f"{os.path.basename(path)} predates the Mapillary extras columns "
            f"(missing {missing}); re-collect or choose a run from 2026-07-23 on."
        )
    return df


def pano_rows(df: pd.DataFrame) -> pd.DataFrame:
    """The 360 panos with usable positions — the rows the estimator measures."""
    out = df[df["is_pano"].fillna(False).astype(bool)]
    out = out[out["status"].isin(PRESENT_STATUSES)]
    return out.dropna(subset=["pano_lat", "pano_lon", "sequence_id"])


def grid_coverage_pct(df: pd.DataFrame) -> float | None:
    """
    360 grid coverage: share of the run's grid points that carry a 360 pano.

    Computed the way json_summarizer does — distinct (query_lat, query_lon)
    pairs with a PRESENT status, over all distinct pairs — so the writeup can
    cite a coverage figure that traces to this committed record instead of to
    `runs.coverage_rate_pct` in the local catalog, which is never published
    (CLAUDE.md, "Notes": a number that lives only in a transcript is the
    single-copy failure the committed record exists to prevent).

    None when the run CSV predates the query_* columns.
    """
    if not {"query_lat", "query_lon"}.issubset(df.columns):
        return None
    points = df[["query_lat", "query_lon"]]
    total = len(points.drop_duplicates())
    if not total:
        return None
    present = points[df["status"].isin(PRESENT_STATUSES).to_numpy()]
    return round(100.0 * len(present.drop_duplicates()) / total, 2)


def project(df: pd.DataFrame):
    """Project to the city's UTM zone; returns (x, y) metre arrays and the frame."""
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(df["pano_lon"], df["pano_lat"]), crs=WGS84)
    gdf = gdf.to_crs(gdf.estimate_utm_crs())
    return gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy(), gdf


def nn_within_group(x: np.ndarray, y: np.ndarray, chunk: int = 256) -> np.ndarray:
    """
    Exact nearest-neighbour distance inside one group, self excluded.

    Brute force, chunked so a long sequence cannot allocate an n-by-n matrix.
    Sequences are small (typically hundreds of images), so this is cheaper than
    building a spatial index per sequence.
    """
    pts = np.column_stack([x, y])
    out = np.empty(len(pts))
    for s in range(0, len(pts), chunk):
        block = pts[s : s + chunk]
        d = np.hypot(block[:, 0, None] - pts[None, :, 0], block[:, 1, None] - pts[None, :, 1])
        d[np.arange(len(block)), np.arange(s, s + len(block))] = np.inf
        out[s : s + len(block)] = d.min(axis=1)
    return out


def pooled_nn(gdf) -> np.ndarray:
    """
    Nearest neighbour over ALL panos regardless of sequence.

    Published as the size of the contamination, not as a spacing metric — see the
    module docstring. Uses the same sjoin_nearest idiom as
    scripts/grid_density_analyze.py:offset_arrays so the two studies' pooled
    numbers are computed identically.
    """
    import geopandas as gpd

    return (
        gpd.sjoin_nearest(gdf, gdf, how="inner", distance_col="_d", exclusive=True)
        .groupby(level=0)["_d"]
        .min()
        .to_numpy()
    )


def hist(values: np.ndarray, bins: np.ndarray = SPACING_BINS) -> dict:
    """
    Binned distribution plus the tail past the last edge.

    Last-edge convention: np.histogram closes the final bin on the RIGHT, so a
    value exactly equal to bins[-1] lands in the last bin and the tail counts
    only values strictly above it. grid_density_analyze.distance_histogram uses
    the opposite convention (>= edges[-1] is tail). The two are self-consistent
    but NOT interchangeable — do not read one module's bins with the other's
    helper.
    """
    counts, _ = np.histogram(values, bins=bins)
    return {
        "bin_edges": [round(float(b), 3) for b in bins],
        "counts": [int(c) for c in counts],
        "n_above_last_edge": int((values > bins[-1]).sum()),
        "n_total": int(len(values)),
    }


def pcts(a: np.ndarray) -> dict:
    if not len(a):
        return {}
    return {f"p{q}": round(float(np.percentile(a, q)), 2) for q in (5, 10, 25, 50, 75, 90, 95, 99)}


def histogram_shares(h: dict) -> dict:
    """
    The shares docs/experiments/pano-spacing.md quotes that a committed
    histogram can express exactly — so the writeup's numbers trace to the JSON
    rather than to a transcript (CLAUDE.md, "Notes").

    Every cut here (1.0 m, 20.0 m) lands ON a SPACING_BINS edge, which is what
    makes the recomputation exact. `stationary_pct` deliberately does NOT live
    here: its threshold is the city's own `quantization_m` (~0.40-0.47 m at
    these latitudes, ~0.6 m near the equator), which falls strictly INSIDE the
    first 0.5 m bin, so a histogram-derived cut could only ever return "share
    below 0.5 m" — a fixed cut wearing a per-city label, and one that silently
    excludes the genuinely sub-quantization [0.5, 0.6) band for an equatorial
    city. It is computed from the raw distances in analyze_city and stored as a
    scalar instead, on the same footing as the percentiles beside it.

    Named histogram_shares, not spacing_shares: grid_density_analyze.py has its
    own spacing_shares taking different arguments and returning different keys,
    and two same-named helpers over near-identical data is how one module's
    semantics get applied to the other's.
    """
    edges = np.asarray(h["bin_edges"], dtype=float)
    counts = np.asarray(h["counts"], dtype=float)
    centers = (edges[:-1] + edges[1:]) / 2.0
    n = h["n_total"]
    if not n:
        raise ValueError("histogram_shares needs a non-empty histogram")
    tail = h["n_above_last_edge"]
    peak = int(np.argmax(counts))
    return {
        "under_1m_pct": 100.0 * float(counts[centers < 1.0].sum()) / n,
        # The tail past the last edge is beyond 20 m too — dropping it would
        # under-report exactly the highway captures this share exists to show.
        "beyond_20m_pct": 100.0 * (float(counts[centers > 20.0].sum()) + tail) / n,
        "peak_share_pct": 100.0 * float(counts[peak]) / n,
        "peak_m": float(centers[peak]),
        "n_total": int(n),
    }


def stationary_share_pct(within: np.ndarray, quantization_m_: float) -> float:
    """
    Share of captures whose nearest in-sequence neighbour sits at or below one
    tile-coordinate unit — i.e. the camera did not measurably move.

    Takes the RAW distances, because the threshold is finer than the histogram
    bin width; see histogram_shares for why that distinction is load-bearing.
    """
    if not len(within):
        raise ValueError("stationary_share_pct needs at least one distance")
    return round(100.0 * float((within <= quantization_m_).sum()) / len(within), 2)


def within_sequence_spacing(df: pd.DataFrame, x: np.ndarray, y: np.ndarray) -> tuple:
    """
    Within-sequence NN distance for every pano in a long-enough sequence.

    Returns (distances, positional index into df) so callers can stratify the
    result by any per-image column without recomputing.
    """
    seq_codes = pd.factorize(df["sequence_id"].to_numpy())[0]
    order = np.argsort(seq_codes, kind="stable")
    sorted_codes = seq_codes[order]
    # Group boundaries in the sorted order.
    bounds = np.flatnonzero(np.diff(sorted_codes)) + 1
    groups = np.split(order, bounds)

    dists, idx = [], []
    for g in groups:
        if len(g) < MIN_SEQUENCE_LEN:
            continue
        dists.append(nn_within_group(x[g], y[g]))
        idx.append(g)
    if not dists:
        return np.array([]), np.array([], dtype=int)
    return np.concatenate(dists), np.concatenate(idx)


def analyze_city(path: str) -> dict:
    name = city_id_from_run(path)
    logger.info("loading %s", name)
    raw = load_census(path)
    coverage_pct = grid_coverage_pct(raw)
    df = pano_rows(raw).reset_index(drop=True)
    del raw
    x, y, gdf = project(df)
    lat0 = float(df["pano_lat"].mean())

    seq_sizes = df.groupby("sequence_id").size()
    within, within_idx = within_sequence_spacing(df, x, y)
    if not len(within):
        # No sequence reaches MIN_SEQUENCE_LEN (a small city, or a run whose
        # sequence_id is mostly null). The estimator this study exists for is
        # undefined; emitting empty percentile blocks instead would defer the
        # failure to a KeyError deep in make_figures and write a broken record
        # on the way there.
        raise SystemExit(
            f"{name}: no sequence reaches {MIN_SEQUENCE_LEN} images, so the "
            "within-sequence estimator is undefined for this run."
        )
    pooled = pooled_nn(gdf)

    logger.info(
        "%s: %d panos, %d sequences, within-seq p50 %.2f m, pooled p50 %.2f m",
        name,
        len(df),
        len(seq_sizes),
        float(np.median(within)) if len(within) else float("nan"),
        float(np.median(pooled)) if len(pooled) else float("nan"),
    )

    # Stratify the within-sequence distances by capture setup. These are the
    # columns that describe the rig, and the hypothesis the study exists to test
    # is that they explain the spread.
    sub = df.iloc[within_idx]
    # `on_foot` is NULLABLE, and null means the tile omitted the field — mode
    # UNKNOWN, not "vehicle". Folding unknowns into the vehicle stratum would
    # contaminate the vehicle median that the pedestrian-vs-vehicle finding
    # rests on, undetectably from the committed record. json_summarizer.py
    # divides by the non-null count for exactly this reason, so unknowns are
    # excluded from BOTH strata here and n_foot_known is published beside the
    # share so a future contaminated city is visible in the JSON.
    foot_true = sub["on_foot"].fillna(False).astype(bool).to_numpy()
    foot_known = sub["on_foot"].notna().to_numpy()
    strata = {
        "vehicle": within[foot_known & ~foot_true],
        "on_foot": within[foot_true],
        "organization": within[sub["organization_id"].notna().to_numpy()],
        "individual": within[sub["organization_id"].isna().to_numpy()],
    }

    return {
        "city": name,
        "run_file": os.path.basename(path),
        "n_panos": int(len(df)),
        "n_sequences": int(len(seq_sizes)),
        "n_sequences_analyzed": int((seq_sizes >= MIN_SEQUENCE_LEN).sum()),
        "min_sequence_len": MIN_SEQUENCE_LEN,
        "sequence_len": {
            "p50": int(seq_sizes.median()),
            "p90": int(seq_sizes.quantile(0.9)),
            "max": int(seq_sizes.max()),
        },
        "quantization_m": round(quantization_m(lat0), 3),
        # From the RAW distances: the threshold is finer than the 0.5 m bins, so
        # this is the one writeup share a histogram cannot express — see
        # histogram_shares.
        "stationary_pct": stationary_share_pct(within, quantization_m(lat0)),
        # The grid-coverage figure the writeup's Implications section quotes.
        # Published here so it traces to committed code + committed data rather
        # than to the unpublished catalog.
        "grid_coverage_pct": coverage_pct,
        "n_foot_known": int(df["on_foot"].notna().sum()),
        "share_on_foot_pct": (
            round(100.0 * float(df["on_foot"].dropna().mean()), 2)
            if bool(df["on_foot"].notna().any())
            else None
        ),
        "share_organization_pct": round(100.0 * df["organization_id"].notna().mean(), 2),
        "within_sequence_m": pcts(within),
        "pooled_m": pcts(pooled),
        "strata_m": {k: pcts(v) | {"n": int(len(v))} for k, v in strata.items()},
        "distributions": {
            "within_sequence_m": hist(within),
            "pooled_m": hist(pooled),
            **{f"stratum_{k}_m": hist(v) for k, v in strata.items() if len(v)},
        },
    }


# Provider palette (two slots, validated: normal-vision dE 33.6, worst-CVD 24.7,
# both >= 3:1 on the surface). Per-city hues reuse the grid-density set so the two
# writeups' figures read as one system — which is why the palette itself lives in
# experiment_style rather than being copied here.
PROVIDER_COLORS = {"gsv": CATEGORICAL[0], "mapillary": CATEGORICAL[1]}
CITY_COLORS = list(CATEGORICAL)

GSV_METRICS = os.path.join(_REPO_ROOT, "docs", "experiments", "grid-density_metrics.json")

# The committed record beside the writeup. write_docs_record is its ONLY
# producer — CLAUDE.md requires the JSON a writeup cites to be written by
# committed code, and `generated_by` below must stay a command the repo can
# actually run.
DOCS_METRICS_NAME = "pano-spacing_metrics.json"

# The working combined file dropped in the gitignored --out-dir. It MUST NOT
# share DOCS_METRICS_NAME: it carries no `_about` block, so `--out-dir
# docs/experiments` would otherwise overwrite the committed record with a
# provenance-less copy and break the producer contract above.
# grid_density_analyze.py keeps the two apart the same way.
WORKING_METRICS_NAME = "combined_metrics.json"
DOCS_FIGURE_PREFIX = "pano-spacing-"
DOCS_GENERATED_BY = "scripts/pano_spacing_analyze.py --docs-dir docs/experiments"


def _style_axis(ax):
    # No y-grid: these are density and dot-and-whisker panels, and horizontal
    # rules would run straight through the interval rows. grid-density's panels
    # want the opposite, which is why experiment_style takes a flag.
    style_axis(ax, ygrid=False)


def pct_from_hist(h: dict, q: float) -> float:
    """
    Percentile recovered from a committed histogram.

    Lets the GSV arm supply p10/p25/p50/p75/p90 on the same footing as Mapillary
    even though grid-density_metrics.json stores only p25-p90 as scalars — the
    figures must not compare a stored percentile against a derived one.

    CLAMPS, and says so. The denominator includes n_above_last_edge, so `cum`
    never reaches 100 when the distribution has mass past the last edge, and
    np.interp then returns the last edge for any quantile above cum.max() —
    drawing a whisker flush against the histogram frame as if it were measured.
    Only GSV's missing p10 takes this path today, so the warning is the guard:
    a silent clamp is indistinguishable from a real percentile in the figure.
    """
    edges = np.asarray(h["bin_edges"], dtype=float)
    counts = np.asarray(h["counts"], dtype=float)
    cum = 100.0 * np.cumsum(counts) / h["n_total"]
    if q > cum[-1] or q < cum[0]:
        logger.warning(
            "p%s is outside the histogram's representable range (%.2f-%.2f%%); "
            "clamping to the bin edge — the figure will show a bound, not a percentile",
            q,
            float(cum[0]),
            float(cum[-1]),
        )
    return float(np.interp(q, cum, edges[1:]))


def _interval_row(ax, y, h, color, stats=None, label_fmt="{:.2f} m"):
    """
    One dot-and-whisker row: p10-p90 whisker, p25-p75 bar, p50 dot.

    Prefers the EXACT stored percentiles and falls back to deriving them from the
    histogram only for keys a metrics file does not carry (grid-density stores
    p25-p90 but no p10). Deriving all of them would put a figure label ~0.1 m off
    the same number in the writeup's table — 0.5 m bins cannot resolve finer —
    and a reader comparing the two would be right to distrust both.
    """
    stats = stats or {}
    p10, p25, p50, p75, p90 = (
        stats.get(f"p{q}", None) if stats.get(f"p{q}") is not None else pct_from_hist(h, q)
        for q in (10, 25, 50, 75, 90)
    )
    ax.plot([p10, p90], [y, y], color=color, linewidth=1.4, alpha=0.55, solid_capstyle="round")
    ax.plot([p25, p75], [y, y], color=color, linewidth=6, alpha=0.85, solid_capstyle="butt")
    ax.plot(
        [p50], [y], "o", color=color, markersize=7, markeredgecolor=SURFACE, markeredgewidth=1.5
    )
    ax.annotate(
        label_fmt.format(p50),
        xy=(p90, y),
        xytext=(8, -3),
        textcoords="offset points",
        color=color,
        fontsize=9,
        fontweight="bold",
    )
    return p50


def make_figures(cities: dict, fig_dir: str, gsv_metrics_path: str = GSV_METRICS) -> list[str]:
    """The four spacing figures. Reads the GSV arm from the grid-density metrics."""
    plt = agg_pyplot()

    os.makedirs(fig_dir, exist_ok=True)
    written = []
    with open(gsv_metrics_path, encoding="utf-8") as fh:
        gsv = json.load(fh)["areas"]

    # 1. The provider comparison. A dot-and-whisker row per city rather than six
    # overlapping density curves: the question is location AND spread across six
    # groups, which an interval plot answers at a glance and a density pile-up
    # does not.
    rows = [
        (
            f"{gsv[k]['label'].split('(')[0].strip()}",
            "gsv",
            gsv[k]["distributions"]["pano_nearest_neighbor_m"],
            gsv[k]["offsets"]["pano_nearest_neighbor_m"],
        )
        for k in ("adrian", "corvallis", "seattle")
    ] + [
        (
            c.split("--")[0].replace("-", " ").title(),
            "mapillary",
            r["distributions"]["within_sequence_m"],
            r["within_sequence_m"],
        )
        for c, r in sorted(cities.items(), key=lambda kv: kv[1]["within_sequence_m"]["p50"])
    ]

    fig, ax = plt.subplots(figsize=(7.6, 4.6), facecolor=SURFACE)
    _style_axis(ax)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    for i, (_label, provider, h, stats) in enumerate(rows):
        _interval_row(ax, len(rows) - 1 - i, h, PROVIDER_COLORS[provider], stats)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in reversed(rows)], fontsize=9, color=INK_2)
    ax.set_xlim(0, 27)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xlabel("Distance to the adjacent capture point (m)", color=INK_2, fontsize=10)
    handles = [
        plt.Line2D(
            [],
            [],
            color=PROVIDER_COLORS["gsv"],
            linewidth=6,
            label="GSV — official panos, nearest neighbour",
        ),
        plt.Line2D(
            [],
            [],
            color=PROVIDER_COLORS["mapillary"],
            linewidth=6,
            label="Mapillary — 360 panos, within-sequence",
        ),
    ]
    # Legend upper-right: the widest row (Budapest) runs to ~26 m along the
    # bottom, so a lower-right legend sits on top of its p90 whisker and label.
    ax.legend(handles=handles, frameon=False, fontsize=9, labelcolor=INK_2, loc="upper right")
    ax.set_title(
        "Mapillary samples finer than GSV — and one city more regularly",
        color=INK,
        fontsize=11,
        loc="left",
        pad=20,
    )
    # Sits in the gap the title's pad opens up, not above the title.
    ax.annotate(
        "dot = median · bar = p25–p75 · whisker = p10–p90",
        xy=(0, 1.0),
        xycoords="axes fraction",
        xytext=(0, 5),
        textcoords="offset points",
        color=INK_2,
        fontsize=8.5,
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, DOCS_FIGURE_PREFIX + "provider_comparison.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 2. The SHAPE of the within-sequence distribution, which is where the three
    # capture regimes are visible and an interval plot cannot show them. Y is
    # clipped: Hamtramck's fleet capture is so regular that its peak is ~77% in a
    # single 0.5 m bin (SPACING_BINS), which on a shared linear axis flattens the
    # other two cities into the baseline. The clipped peak is annotated with its
    # true height rather than silently cropped.
    fig, ax = plt.subplots(figsize=(7.6, 4.6), facecolor=SURFACE)
    _style_axis(ax)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    y_clip = 16.0
    for i, (c, r) in enumerate(sorted(cities.items())):
        color = CITY_COLORS[i % len(CITY_COLORS)]
        name = c.split("--")[0].replace("-", " ").title()
        h = r["distributions"]["within_sequence_m"]
        edges = np.asarray(h["bin_edges"], dtype=float)
        centers = (edges[:-1] + edges[1:]) / 2.0
        share = 100.0 * np.asarray(h["counts"], dtype=float) / h["n_total"]
        ax.plot(
            centers,
            share,
            drawstyle="steps-mid",
            color=color,
            linewidth=2,
            label=f"{name} (p50 {r['within_sequence_m']['p50']:.2f} m)",
        )
        # Through histogram_shares so the annotated peak is literally the number
        # the writeup quotes and the test pins, not a parallel computation.
        sh = histogram_shares(h)
        if sh["peak_share_pct"] > y_clip:
            ax.annotate(
                f"{name} peaks at {sh['peak_share_pct']:.0f}%\nin one 0.5 m bin",
                xy=(sh["peak_m"], y_clip),
                xytext=(14, -34),
                textcoords="offset points",
                color=color,
                fontsize=8.5,
                fontweight="bold",
                arrowprops={"arrowstyle": "->", "color": color, "alpha": 0.7},
            )
    ax.set_xlim(0, 25)
    ax.set_ylim(0, y_clip)
    ax.set_xlabel(
        "Within-sequence distance to the adjacent capture point (m)", color=INK_2, fontsize=10
    )
    ax.set_ylabel("Share of panos (% per 0.5 m bin)", color=INK_2, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper right")
    ax.set_title(
        "Three capture regimes: regulated, urban-dense, and mixed",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, DOCS_FIGURE_PREFIX + "within_sequence_shape.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 3. What pooling costs. Same interval form as figure 1 rather than another
    # density pile-up: the claim is about a shift in location, and two rows per
    # city make the collapse legible without the axis fighting Hamtramck's spike.
    fig, ax = plt.subplots(figsize=(7.6, 4.0), facecolor=SURFACE)
    _style_axis(ax)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    pool_rows = []
    for c, r in sorted(cities.items()):
        name = c.split("--")[0].replace("-", " ").title()
        pool_rows.append(
            (
                f"{name} — within-sequence",
                "#2a78d6",
                r["distributions"]["within_sequence_m"],
                r["within_sequence_m"],
            )
        )
        pool_rows.append(
            (f"{name} — pooled", "#eb6834", r["distributions"]["pooled_m"], r["pooled_m"])
        )
    for i, (_label, color, h, stats) in enumerate(pool_rows):
        _interval_row(ax, len(pool_rows) - 1 - i, h, color, stats)
    ax.set_yticks(range(len(pool_rows)))
    ax.set_yticklabels([r[0] for r in reversed(pool_rows)], fontsize=9, color=INK_2)
    ax.set_xlim(0, 27)
    ax.set_ylim(-0.7, len(pool_rows) - 0.3)
    ax.set_xlabel("Distance to the adjacent capture point (m)", color=INK_2, fontsize=10)
    ax.set_title(
        "Pooling across contributors collapses the interval",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, DOCS_FIGURE_PREFIX + "pooled_vs_sequence.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 4. Capture setup. The hypothesis the study exists to test is that the rig
    # explains the spread, so the strata get their own comparison.
    strata_rows = []
    for i, (c, r) in enumerate(sorted(cities.items())):
        name = c.split("--")[0].replace("-", " ").title()
        for key, pretty in (
            ("vehicle", "vehicle"),
            ("on_foot", "on foot"),
            ("organization", "organization"),
            ("individual", "individual"),
        ):
            h = r["distributions"].get(f"stratum_{key}_m")
            if h and h["n_total"] >= 1000:
                strata_rows.append(
                    (
                        f"{name} — {pretty}",
                        CITY_COLORS[i % len(CITY_COLORS)],
                        h,
                        r["strata_m"].get(key, {}),
                    )
                )

    fig, ax = plt.subplots(figsize=(7.6, 5.4), facecolor=SURFACE)
    _style_axis(ax)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    for i, (_label, color, h, stats) in enumerate(strata_rows):
        _interval_row(ax, len(strata_rows) - 1 - i, h, color, stats)
    ax.set_yticks(range(len(strata_rows)))
    ax.set_yticklabels([r[0] for r in reversed(strata_rows)], fontsize=9, color=INK_2)
    ax.set_xlim(0, 33)
    ax.set_ylim(-0.7, len(strata_rows) - 0.3)
    ax.set_xlabel(
        "Within-sequence distance to the adjacent capture point (m)", color=INK_2, fontsize=10
    )
    ax.set_title(
        "Capture setup moves the interval more than the city does",
        color=INK,
        fontsize=11,
        loc="left",
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, DOCS_FIGURE_PREFIX + "capture_setup.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)
    return written


def write_docs_record(
    results: list[dict], docs_dir: str, gsv_metrics: str = GSV_METRICS
) -> list[str]:
    """
    Write the durable record committed beside the writeup: the merged metrics
    JSON (every city, including the binned histograms) and the figures under
    docs_dir/figures. The ONLY producer of those paths.
    """
    # Fail BEFORE writing anything. The figures are the last step and they read
    # the GSV arm from a separate file; if that read raises, a record written
    # first is left with a new JSON beside stale figures — a half-updated
    # committed record is worse than an unwritten one.
    if not os.path.exists(gsv_metrics):
        raise SystemExit(
            f"cannot write the committed record: {gsv_metrics} is missing, and the "
            "figures need the GSV arm. Run from the repo checkout, or pass --gsv-metrics."
        )
    os.makedirs(docs_dir, exist_ok=True)
    cities = {r["city"]: r for r in results}
    payload = {
        "_about": {
            "experiment": "pano-spacing",
            "writeup": "docs/experiments/pano-spacing.md",
            "generated_by": DOCS_GENERATED_BY,
            "note": (
                "Committed metrics for the capture-spacing study. The Mapillary census "
                "CSVs stay in the gitignored /experiments/pano-spacing/ (pulled from "
                "prod; any run from 2026-07-23 carries the extras columns); this file "
                "is the durable record of the derived numbers the writeup cites. "
                "distributions[] carries 0.5 m histograms so the figures are "
                "reproducible without the census, and histogram_shares() recomputes "
                "the bin-aligned shares the writeup quotes from those bins. "
                "stationary_pct and the percentiles are stored scalars computed "
                "from the raw distances, because their thresholds are finer than "
                "the bin width."
            ),
        },
        "cities": cities,
    }
    json_path = os.path.join(docs_dir, DOCS_METRICS_NAME)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return [json_path, *make_figures(cities, os.path.join(docs_dir, "figures"), gsv_metrics)]


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="GSV vs Mapillary capture-spacing analysis.")
    parser.add_argument("--in-dir", default=DEFAULT_IO_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_IO_DIR)
    parser.add_argument("--city", default="all", help="substring match on the run filename")
    parser.add_argument(
        "--figures-from-metrics",
        metavar="PATH",
        help=(
            "Regenerate the figures from a committed pano-spacing_metrics.json and "
            "exit. Needs no census CSVs — they are hundreds of MB and gitignored, "
            "so the figures must survive without them."
        ),
    )
    parser.add_argument(
        "--docs-dir",
        metavar="DIR",
        help=(
            "Also write the durable record committed beside the writeup: the "
            "merged metrics JSON and the prefixed figures. Requires the full city "
            "set, since the writeup's cross-city claims are only meaningful over "
            "all of them."
        ),
    )
    parser.add_argument("--gsv-metrics", default=GSV_METRICS)
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level), format="%(asctime)s - %(levelname)s - %(message)s"
    )

    if args.figures_from_metrics:
        with open(args.figures_from_metrics, encoding="utf-8") as fh:
            metrics = json.load(fh)
        fig_dir = os.path.join(os.path.dirname(args.figures_from_metrics), "figures")
        for p in make_figures(metrics["cities"], fig_dir, args.gsv_metrics):
            print(f"Figure: {p}")
        return 0

    if args.docs_dir and args.city != "all":
        # A committed record built from a subset would silently under-report the
        # cross-city spread that is the writeup's main claim.
        raise SystemExit("--docs-dir requires --city all")

    paths = sorted(glob.glob(os.path.join(args.in_dir, "*_mapillary_*.csv.gz")))
    # The glob also matches streetwalk snapshots and diff files; naming.py is the
    # single source of truth for which of them is a grid run.
    skipped = [p for p in paths if not is_mapillary_run(p)]
    paths = [p for p in paths if is_mapillary_run(p)]
    for p in skipped:
        logger.info("skipping non-run artifact %s", os.path.basename(p))
    if args.city != "all":
        paths = [p for p in paths if args.city in os.path.basename(p)]
    if not paths:
        raise SystemExit(f"no Mapillary run CSVs matching {args.city!r} in {args.in_dir}")

    by_city: dict[str, list[str]] = {}
    for p in paths:
        by_city.setdefault(city_id_from_run(p), []).append(p)
    dupes = {c: sorted(os.path.basename(p) for p in v) for c, v in by_city.items() if len(v) > 1}
    if dupes:
        # Everything downstream keys on city, so a second run date would be
        # dropped from the record with the last glob entry silently winning.
        raise SystemExit(f"more than one run per city in {args.in_dir}: {dupes}")

    if args.docs_dir:
        # --city all only means "do not filter the glob"; it says nothing about
        # what is actually on disk. Without this, a half-populated --in-dir
        # writes a committed record covering fewer cities and exits 0 — the very
        # under-reporting the --city guard above claims to prevent.
        absent = sorted(set(STUDY_CITIES) - set(by_city))
        if absent:
            raise SystemExit(
                f"--docs-dir needs every study city; {absent} missing from {args.in_dir}"
            )

    os.makedirs(args.out_dir, exist_ok=True)
    results = []
    for p in paths:
        r = analyze_city(p)
        results.append(r)
        with open(os.path.join(args.out_dir, f"{r['city']}_metrics.json"), "w") as fh:
            json.dump(r, fh, indent=2)

    combined = os.path.join(args.out_dir, WORKING_METRICS_NAME)
    with open(combined, "w") as fh:
        json.dump({"cities": {r["city"]: r for r in results}}, fh, indent=2)
    print(f"Metrics: {combined}")

    if args.docs_dir:
        for path in write_docs_record(results, args.docs_dir, args.gsv_metrics):
            print(f"Committed record: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
