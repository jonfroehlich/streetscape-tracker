"""
Work lists for the GSV capture-history crawler (issue #442).

A work list is one road walk's sample points, re-derived from the frozen
network exactly as ``street_coverage.compute_streetwalk_coverage`` derives them
(``road_sampling.generate_samples`` + the ``quantize_coord`` join to the walk's
collected rows), with each sample's current official pano as its *anchor* --
under the walk's own covered rule: PRESENT, ``© Google`` exactly
(``analysis.is_google_copyright``), and within the walk's ``match_dist_m`` of
the sample, measured in the same metric CRS coverage measures it in. A pano the
walk scores as too far is not an anchor; its row reads ``OUT_OF_RADIUS``, the
grid loader's word for the same thing (issue #367). The grid's 50 m
``GSV_QUERY_RADIUS_M`` is NOT the rule here: the walk bounds sample-to-pano
distance itself, at 25 m by default, and the anchor is the walk's pano.
It is built on the catalog host and shipped to the crawler host, which holds
no catalog: the file is the whole contract, and its schema is
``download_gsv_history.WORKLIST_COLUMNS``.

Pass 1 is each edge's midpoint sample (``sample_idx == edge_samples // 2``),
pass 2 the rest, so the two passes query disjoint points and together cost no
more than pass 2 alone. Coincident samples on touching edges collapse to the
first, the rule ``dedupe_query_points`` applies at collection; the other edge's
sample is recoverable by re-deriving the samples, exactly as coverage is.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd

from streetscape_metadata_tracker.analysis import (
    OUT_OF_RADIUS,
    PRESENT_STATUSES,
    is_google_copyright,
)
from streetscape_metadata_tracker.download_gsv_history import WORKLIST_COLUMNS

from .road_sampling import generate_samples, quantize_coord
from .street_coverage import DEFAULT_MATCH_DIST_M, WGS84


def build_worklist(
    edges: gpd.GeoDataFrame,
    collected: pd.DataFrame,
    *,
    city_id: str,
    walk_csv_filename: str,
    network_type: str,
    spacing_m: float,
    match_dist_m: float = DEFAULT_MATCH_DIST_M,
    limit: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    One work-list row per sample point of the walk, with its anchor pano.

    Args:
        edges: the frozen network's edge frame (``graph_to_edges``), WGS84 with
            ``edge_id``.
        collected: the walk's collected rows as ``fileutils.load_city_csv_file``
            returns them (``query_lat/query_lon/status/pano_id/capture_date/
            copyright_info/pano_lat/pano_lon``).
        match_dist_m: the walk's own covered threshold (``street_walks.match_dist_m``);
            a present pano farther than this from its sample is not an anchor.
        limit: keep only the first ``limit`` edges, whole, in sample order (a
            pilot's "few thousand requests").

    Returns:
        ``(worklist, stats)``; ``stats`` counts edges, samples, the pass split,
        coincident samples dropped, samples with no collected row, present
        panos too far to anchor, and the anchored share.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    samples = generate_samples(edges, spacing_m)
    if limit is not None:
        keep = samples["edge_id"].drop_duplicates().head(limit)
        samples = samples[samples["edge_id"].isin(keep)].reset_index(drop=True)
    edges_kept = int(samples["edge_id"].nunique())
    samples = samples.copy()
    samples["edge_samples"] = samples.groupby("edge_id")["sample_idx"].transform("size").astype(int)
    samples["pass"] = (samples["sample_idx"] == samples["edge_samples"] // 2).map(
        {True: 1, False: 2}
    )
    samples["_key"] = [
        quantize_coord(la, lo) for la, lo in zip(samples["lat"], samples["lon"], strict=True)
    ]
    before = len(samples)
    samples = samples.drop_duplicates("_key", keep="first").reset_index(drop=True)
    coincident_dropped = before - len(samples)

    coll = collected.copy()
    coll["_key"] = [
        quantize_coord(la, lo) for la, lo in zip(coll["query_lat"], coll["query_lon"], strict=True)
    ]
    coll = coll.drop_duplicates("_key", keep="first")
    for col in ("pano_lat", "pano_lon"):
        if col not in coll.columns:
            coll[col] = float("nan")
    m = samples.merge(
        coll[
            ["_key", "status", "pano_id", "capture_date", "copyright_info", "pano_lat", "pano_lon"]
        ],
        on="_key",
        how="left",
    )
    unmatched = int(m["status"].isna().sum())

    present = m["status"].isin(PRESENT_STATUSES)
    within = _within(edges, m, match_dist_m)
    too_far = present & ~within
    official = present & within & is_google_copyright(m["copyright_info"])
    anchor_pano = m["pano_id"].where(official & m["pano_id"].notna())
    dates = pd.to_datetime(m["capture_date"], errors="coerce")
    anchor_date = dates.dt.strftime("%Y-%m-%d").where(official & dates.notna())
    anchor_status = m["status"].mask(too_far, OUT_OF_RADIUS)

    out = pd.DataFrame(
        {
            "city_id": city_id,
            "walk_csv_filename": walk_csv_filename,
            "network_type": network_type,
            "spacing_m": spacing_m,
            "edge_id": m["edge_id"].astype(str),
            "sample_idx": m["sample_idx"].astype(int),
            "edge_samples": m["edge_samples"].astype(int),
            "pass": m["pass"].astype(int),
            "query_lat": m["lat"].astype(float),
            "query_lon": m["lon"].astype(float),
            "anchor_status": anchor_status,
            "anchor_pano_id": anchor_pano,
            "anchor_capture_date": anchor_date,
            "anchor_copyright": m["copyright_info"],
        },
        columns=WORKLIST_COLUMNS,
    )
    anchored = int(out["anchor_pano_id"].notna().sum())
    stats = {
        "edges": edges_kept,
        "samples": int(len(out)),
        "pass1_rows": int((out["pass"] == 1).sum()),
        "pass2_rows": int((out["pass"] == 2).sum()),
        "coincident_dropped": int(coincident_dropped),
        "unmatched_samples": unmatched,
        "anchors_out_of_radius": int(too_far.sum()),
        "anchored": anchored,
        "anchored_share": round(anchored / len(out), 4) if len(out) else None,
    }
    return out, stats


def _within(edges: gpd.GeoDataFrame, m: pd.DataFrame, match_dist_m: float) -> pd.Series:
    """
    Whether each merged sample's pano lies within ``match_dist_m`` of it --
    the walk's own test, in the same metric CRS ``compute_streetwalk_coverage``
    uses, so a pano it scores as too far is too far here. A missing pano
    position is False (NaN <= x is False), never within.
    """
    if m.empty:
        return pd.Series([], dtype=bool, index=m.index)
    metric_crs = edges.estimate_utm_crs()
    sample_pts = gpd.GeoSeries(gpd.points_from_xy(m["lon"], m["lat"]), crs=WGS84).to_crs(metric_crs)
    pano_pts = gpd.GeoSeries(
        gpd.points_from_xy(
            pd.to_numeric(m["pano_lon"], errors="coerce"),
            pd.to_numeric(m["pano_lat"], errors="coerce"),
        ),
        crs=WGS84,
    ).to_crs(metric_crs)
    return pd.Series((sample_pts.distance(pano_pts) <= match_dist_m).to_numpy(), index=m.index)
