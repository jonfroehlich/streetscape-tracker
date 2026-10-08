"""
On-street sample-point generation for the road-walk collector (issue #99).

The road-walk modality queries the provider for the nearest pano at fixed
spacing along each OSM street centerline (rather than on a grid lattice). This
module turns a frozen-network edge GeoDataFrame into the set of on-street query
points, plus the ``(edge_id, sample_idx) -> location`` layout the coverage step
re-derives.

Determinism is the whole point: sample points are a pure function of the frozen
GraphML (issue #103) and ``spacing_m``. The same frozen network yields the
identical points every run, so run-to-run road-walk coverage is comparable
without a separate frozen-points store — a ``--refresh`` that re-freezes the
network is what changes the points. No network access happens here.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

WGS84 = "EPSG:4326"

# Coordinate quantization for matching a sample point back to its collected
# metadata row. Mirrors download_gsv.resume_point_key (round to 9 decimals,
# ~0.1 mm): far finer than any spacing, far coarser than the one-ULP noise of a
# reader that does not parse round-trip (the loader does since #425). Defined locally so the coverage/sampling code
# (geopandas-only) need not import the aiohttp-heavy downloader module.
_COORD_QUANT_DECIMALS = 9


def quantize_coord(lat: float, lon: float) -> tuple[float, float]:
    """Rounded (lat, lon) key for matching samples to collected rows."""
    return (round(lat, _COORD_QUANT_DECIMALS), round(lon, _COORD_QUANT_DECIMALS))


def generate_samples(edges: gpd.GeoDataFrame, spacing_m: float) -> pd.DataFrame:
    """
    Place on-street sample points along every edge at ~``spacing_m`` spacing.

    Each edge of metric length ``L`` gets ``n = max(1, round(L / spacing_m))``
    points at offsets ``(k + 0.5) * L / n`` for ``k`` in ``0..n-1`` — evenly
    spaced and centered, so points sit in the edge interior (never exactly on a
    shared intersection node) and every edge, however short, gets at least its
    midpoint. The actual spacing stays within [spacing_m/2, spacing_m] of the
    target.

    Args:
        edges: WGS84 LineString GeoDataFrame carrying a stable ``edge_id``
            column (from ``download_street_network.graph_to_edges``).
        spacing_m: target along-edge spacing in metres.

    Returns:
        DataFrame with columns ``edge_id`` (str), ``sample_idx`` (int),
        ``lat``, ``lon`` (WGS84), one row per sample, in deterministic
        (edge order, then along-edge) order.
    """
    if spacing_m <= 0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")
    if len(edges) == 0:
        return pd.DataFrame(
            {
                "edge_id": pd.Series([], dtype=object),
                "sample_idx": pd.Series([], dtype=int),
                "lat": pd.Series([], dtype=float),
                "lon": pd.Series([], dtype=float),
            }
        )

    # Work in a local metric CRS so offsets/spacing are in metres. Same single
    # estimated UTM zone the coverage step uses; cities are far smaller than a
    # zone, so it's accurate to well under the spacing.
    metric_crs = edges.estimate_utm_crs()
    edges_m = edges.to_crs(metric_crs)

    edge_ids: list[str] = []
    sample_idxs: list[int] = []
    points_m = []
    for edge_id, geom in zip(edges["edge_id"], edges_m.geometry, strict=True):
        if geom is None or geom.is_empty:
            continue
        length = geom.length
        if length <= 0:
            continue
        n = max(1, round(length / spacing_m))
        for k in range(n):
            offset = (k + 0.5) * length / n
            edge_ids.append(edge_id)
            sample_idxs.append(k)
            points_m.append(geom.interpolate(offset))

    if not points_m:
        return pd.DataFrame(
            {
                "edge_id": pd.Series([], dtype=object),
                "sample_idx": pd.Series([], dtype=int),
                "lat": pd.Series([], dtype=float),
                "lon": pd.Series([], dtype=float),
            }
        )

    wgs = gpd.GeoSeries(points_m, crs=metric_crs).to_crs(WGS84)
    return pd.DataFrame(
        {
            "edge_id": edge_ids,
            "sample_idx": sample_idxs,
            "lat": wgs.y.to_numpy(),
            "lon": wgs.x.to_numpy(),
        }
    )


def dedupe_query_points(samples: pd.DataFrame) -> list[tuple[float, float, int, int]]:
    """
    Collapse the sample list into the unique locations to actually query.

    Distinct edges can share a coincident sample location (touching endpoints,
    overlapping ways); querying each unique location once avoids wasted requests
    and duplicate budget spend. Attribution is NOT lost — the coverage step
    re-derives the full ``(edge_id, sample_idx) -> location`` mapping and matches
    every sample back to the single collected row via ``quantize_coord``.

    Returns:
        List of ``(lat, lon, seq, 0)`` tuples for ``download_gsv.collect_points_async``.
        The trailing ints are opaque bookkeeping (only surfaced in the
        ``_failed_points.csv`` diagnostic); ``seq`` is a running index.
    """
    seen: set[tuple[float, float]] = set()
    points: list[tuple[float, float, int, int]] = []
    seq = 0
    for lat, lon in zip(samples["lat"], samples["lon"], strict=True):
        key = quantize_coord(lat, lon)
        if key in seen:
            continue
        seen.add(key)
        points.append((float(lat), float(lon), seq, 0))
        seq += 1
    return points
