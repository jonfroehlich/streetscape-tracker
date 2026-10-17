"""
Work lists for the GSV capture-history crawler (issue #442).

A work list is one road walk's sample points, re-derived from the frozen
network exactly as ``street_coverage.compute_streetwalk_coverage`` derives them
(``road_sampling.generate_samples`` + the ``quantize_coord`` join to the walk's
collected rows), with each sample's current official pano as its *anchor*.
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

from streetscape_metadata_tracker.analysis import PRESENT_STATUSES
from streetscape_metadata_tracker.download_gsv_history import WORKLIST_COLUMNS

from .road_sampling import generate_samples, quantize_coord

GOOGLE_COPYRIGHT = "© Google"


def build_worklist(
    edges: gpd.GeoDataFrame,
    collected: pd.DataFrame,
    *,
    city_id: str,
    walk_csv_filename: str,
    network_type: str,
    spacing_m: float,
    limit: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    One work-list row per sample point of the walk, with its anchor pano.

    Args:
        edges: the frozen network's edge frame (``graph_to_edges``), WGS84 with
            ``edge_id``.
        collected: the walk's collected rows as ``fileutils.load_city_csv_file``
            returns them (``query_lat/query_lon/status/pano_id/capture_date/
            copyright_info``).
        limit: keep only the first ``limit`` edges, whole, in sample order (a
            pilot's "few thousand requests").

    Returns:
        ``(worklist, stats)``; ``stats`` counts edges, samples, the pass split,
        coincident samples dropped, samples with no collected row, and the
        anchored share.
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
    m = samples.merge(
        coll[["_key", "status", "pano_id", "capture_date", "copyright_info"]], on="_key", how="left"
    )
    unmatched = int(m["status"].isna().sum())

    official = m["status"].isin(PRESENT_STATUSES) & (m["copyright_info"] == GOOGLE_COPYRIGHT)
    anchor_pano = m["pano_id"].where(official & m["pano_id"].notna())
    dates = pd.to_datetime(m["capture_date"], errors="coerce")
    anchor_date = dates.dt.strftime("%Y-%m-%d").where(official & dates.notna())

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
            "anchor_status": m["status"],
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
        "anchored": anchored,
        "anchored_share": round(anchored / len(out), 4) if len(out) else None,
    }
    return out, stats
