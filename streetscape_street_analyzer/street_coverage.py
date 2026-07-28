"""
Core street-coverage analysis (no network access).

Given a run's pano DataFrame and an OSM street-edge GeoDataFrame, decide which
street segments have imagery coverage and summarize by `highway` type. The
network fetch lives in `download_street_network`; everything here operates on
already-loaded geometries so it is fully unit-testable without hitting Overpass.

Coverage definition (issue #24, intentionally liberal): a street segment is
"covered" if at least one pano lies within `match_dist_m` metres of it. GSV is a
grid *sample* (one pano per sampled grid point) while Mapillary is a *census*;
the metre threshold — roughly one grid step — lets a street sampled between grid
points still count as covered. This over-counts rather than under-counts, which
matches the issue's ask ("something rather liberal").
"""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
import pandas as pd

from streetscape_metadata_tracker.analysis import FLAT_ONLY, is_google_copyright

from .road_sampling import quantize_coord

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"
DEFAULT_MATCH_DIST_M = 25.0
_YEAR_SECONDS = 365.25 * 24 * 3600

# OSM `highway` values collapsed into a small, ordered set of buckets for the
# by-type breakdown. Anything unrecognized falls through to "other".
#
# Motorized classes: present in every network type, including the default
# 'drive'. These are the only buckets a pre-#164 drive walk can produce.
_MOTORIZED_BUCKETS = [
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "unclassified",
    "service",
    "living_street",
]

# Non-motorized classes, reachable only from a broader network type
# ('all_public', 'all', 'walk', 'bike'). osmnx's 'drive' Overpass filter
# excludes every one of them by name, which is why walks collected before the
# broad-network work could not see alleys, sidewalks, or park trails at all.
_NON_MOTORIZED_BUCKETS = [
    "pedestrian",
    "footway",
    "path",
    "cycleway",
    "steps",
    "track",
    "bridleway",
]

# Subtypes of `highway=service`, read off the `service` tag. 'all_public'
# filters out only `service=private`, so public driveways and parking aisles DO
# come through — bucketing them explicitly keeps them visible and filterable
# rather than silently inflating the plain `service` class with places no
# imagery provider would ever drive. Alleys are the interesting one here: they
# are real streets that the 'drive' filter drops.
_SERVICE_BUCKETS = ["alley", "driveway", "parking_aisle"]

_HIGHWAY_BUCKETS = _MOTORIZED_BUCKETS + _NON_MOTORIZED_BUCKETS + _SERVICE_BUCKETS


def _first_recognized(tag_value: Any, allowed: list[str]) -> str | None:
    """
    First recognized tag in a possibly list-valued OSM tag, or None.

    osmnx emits a list when simplification merged ways carrying different values
    for the tag, so every tag read here has to tolerate both shapes.
    """
    values = tag_value if isinstance(tag_value, (list, tuple)) else [tag_value]
    for value in values:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        tag = str(value).strip().lower()
        if tag.endswith("_link"):
            tag = tag[: -len("_link")]
        if tag in allowed:
            return tag
    return None


def normalize_highway(highway: Any, service: Any = None) -> str:
    """
    Collapse an OSM `highway` (+ optional `service`) tag to a canonical bucket.

    The ``_link`` suffix is dropped, so e.g. ``motorway_link`` counts as
    ``motorway``. When ``highway`` is ``service`` and the ``service`` tag names a
    recognized subtype, that subtype wins: an alley is reported as ``alley``
    rather than folded into the generic ``service`` class.

    ``service`` is optional and defaults to None so pre-existing single-argument
    callers keep working; drive networks cached before the tag was retained have
    no ``service`` column at all.

    Examples:
        >>> normalize_highway("motorway_link")
        'motorway'
        >>> normalize_highway("footway")
        'footway'
        >>> normalize_highway("service", "alley")
        'alley'
        >>> normalize_highway("service", "drive-through")
        'service'
        >>> normalize_highway(["service", "residential"], ["alley"])
        'alley'
        >>> normalize_highway("busway")
        'other'
    """
    bucket = _first_recognized(highway, _HIGHWAY_BUCKETS)
    if bucket is None:
        return "other"
    if bucket == "service":
        subtype = _first_recognized(service, _SERVICE_BUCKETS)
        if subtype is not None:
            return subtype
    return bucket


def _bucket_series(edges: pd.DataFrame) -> pd.Series:
    """
    ``highway_bucket`` for every edge, reading the ``service`` tag when present.

    Older cached drive GraphML has no ``service`` column (graph_to_edges did not
    retain it), and a network with no service roads at all won't have one
    either, so its absence is normal rather than an error.
    """
    if "service" not in edges.columns:
        return edges["highway"].apply(normalize_highway)
    return pd.Series(
        [normalize_highway(h, s) for h, s in zip(edges["highway"], edges["service"], strict=True)],
        index=edges.index,
    )


def select_pano_points(df: pd.DataFrame, provider: str) -> gpd.GeoDataFrame:
    """
    Extract located panos from a run DataFrame as a WGS84 point GeoDataFrame.

    Keeps only ``status == 'OK'`` rows with non-null pano coordinates. For GSV,
    restricts to official Google imagery (exact ``© Google`` match, mirroring the
    stats/JSON/frontend definition); for other providers keeps every pano.
    Retains ``capture_date`` (coerced to datetime) so coverage can be aged.
    """
    mask = (df["status"] == "OK") & df["pano_lat"].notna() & df["pano_lon"].notna()
    if provider == "gsv":
        # A legacy pre-copyright baseline CSV may lack the column entirely (not
        # just be all-NaN); treat that as "no official Google imagery" (empty
        # mask) rather than crashing — analyze._warn_no_panos then explains the
        # 0% result. When present, the exact '© Google' filter applies as usual.
        if "copyright_info" in df.columns:
            mask = mask & is_google_copyright(df["copyright_info"])
        else:
            mask = mask & False

    panos = df.loc[mask, ["pano_lat", "pano_lon", "capture_date"]].copy()
    if not pd.api.types.is_datetime64_any_dtype(panos["capture_date"]):
        panos["capture_date"] = pd.to_datetime(panos["capture_date"], errors="coerce")

    geometry = gpd.points_from_xy(panos["pano_lon"], panos["pano_lat"])
    return gpd.GeoDataFrame(
        panos[["capture_date"]].reset_index(drop=True),
        geometry=geometry,
        crs=WGS84,
    )


def _age_years(capture_date: pd.Timestamp | None, run_ts: pd.Timestamp) -> float | None:
    """Pano age in years relative to the run date, or None if the date is unknown."""
    if capture_date is None or pd.isna(capture_date):
        return None
    return (run_ts - capture_date).total_seconds() / _YEAR_SECONDS


def compute_street_coverage(
    edges: gpd.GeoDataFrame,
    panos: gpd.GeoDataFrame,
    run_date: str,
    match_dist_m: float = DEFAULT_MATCH_DIST_M,
) -> gpd.GeoDataFrame:
    """
    Tag each street edge with coverage against the given panos.

    Args:
        edges: WGS84 LineString GeoDataFrame with a ``highway`` column (from
            osmnx ``graph_to_gdfs``); a ``length`` column (metres) is used if
            present, otherwise segment length is measured in a local UTM CRS.
        panos: WGS84 point GeoDataFrame from `select_pano_points`, carrying
            ``capture_date``.
        run_date: ``YYYY-MM-DD``; ages are pinned to it (deterministic).
        match_dist_m: coverage threshold in metres.

    Returns:
        A copy of ``edges`` (WGS84, RangeIndex) with added columns:
        ``highway_bucket``, ``length_m``, ``covered`` (bool),
        ``nearest_pano_date`` (str|None), ``nearest_pano_age_years`` (float|None).
    """
    run_ts = pd.Timestamp(run_date)
    out = edges.reset_index(drop=True).copy()
    out["highway_bucket"] = _bucket_series(out)

    # An empty network (tiny/invalid bbox, or a network_type that filtered every
    # edge) has no geometry for estimate_utm_crs() to work from — it raises. Return
    # a well-formed 0-edge frame instead; summarize_coverage/build_streets_geojson
    # already handle it, yielding a valid 0-segment (0%) artifact.
    if out.empty:
        out["length_m"] = pd.Series([], dtype=float)
        out["covered"] = pd.Series([], dtype=bool)
        out["nearest_pano_date"] = pd.Series([], dtype=object)
        out["nearest_pano_age_years"] = pd.Series([], dtype=object)
        return out

    # Work in a local metric CRS so distances and lengths are in metres. All
    # cities here are far smaller than a UTM zone, so a single estimated zone is
    # accurate to well under the match threshold.
    metric_crs = out.estimate_utm_crs()
    edges_m = out.to_crs(metric_crs)

    if "length" in out.columns:
        out["length_m"] = pd.to_numeric(out["length"], errors="coerce").fillna(edges_m.length)
    else:
        out["length_m"] = edges_m.length

    out["covered"] = False
    out["nearest_pano_date"] = None
    out["nearest_pano_age_years"] = None

    if len(panos) > 0:
        panos_m = panos.to_crs(metric_crs)
        # Nearest pano per edge within the threshold. sjoin_nearest keeps only
        # edges with a match, and can emit ties (>1 pano at equal distance) as
        # duplicate rows — collapse to the closest, breaking ties by newest date.
        joined = gpd.sjoin_nearest(
            edges_m, panos_m, how="inner", max_distance=match_dist_m, distance_col="_dist"
        )
        if len(joined) > 0:
            joined = joined.sort_values(["_dist", "capture_date"], ascending=[True, False])
            nearest = joined[~joined.index.duplicated(keep="first")]
            out.loc[nearest.index, "covered"] = True
            for edge_idx, cap_date in nearest["capture_date"].items():
                if pd.notna(cap_date):
                    out.at[edge_idx, "nearest_pano_date"] = cap_date.date().isoformat()
                    out.at[edge_idx, "nearest_pano_age_years"] = round(
                        _age_years(cap_date, run_ts), 3
                    )

    return out


def _bucket_order(bucket: str) -> int:
    try:
        return _HIGHWAY_BUCKETS.index(bucket)
    except ValueError:
        return len(_HIGHWAY_BUCKETS)  # "other" sorts last


def summarize_coverage(covered_edges: gpd.GeoDataFrame) -> dict[str, Any]:
    """
    Aggregate per-segment coverage into by-highway-type and overall stats.

    Reports coverage two ways — by segment count and by street length — because a
    city can have most *segments* covered yet a large fraction of *kilometres*
    uncovered (long rural roads), and vice versa.
    """

    def _block(group: pd.DataFrame) -> dict[str, Any]:
        segments = int(len(group))
        covered = group["covered"]
        num_covered = int(covered.sum())
        length_km = float(group["length_m"].sum()) / 1000.0
        length_km_covered = float(group.loc[covered, "length_m"].sum()) / 1000.0
        ages = pd.to_numeric(group.loc[covered, "nearest_pano_age_years"], errors="coerce").dropna()
        return {
            "segments": segments,
            "covered": num_covered,
            "length_km": round(length_km, 3),
            "length_km_covered": round(length_km_covered, 3),
            "coverage_pct_by_count": round(100.0 * num_covered / segments, 1) if segments else 0.0,
            "coverage_pct_by_length": round(100.0 * length_km_covered / length_km, 1)
            if length_km
            else 0.0,
            "median_covered_age_years": round(float(ages.median()), 2) if len(ages) else None,
        }

    by_type = {bucket: _block(group) for bucket, group in covered_edges.groupby("highway_bucket")}
    by_type = dict(sorted(by_type.items(), key=lambda kv: _bucket_order(kv[0])))

    totals = _block(covered_edges)
    totals["uncovered"] = totals["segments"] - totals["covered"]
    totals["uncovered_pct_by_count"] = round(100.0 - totals["coverage_pct_by_count"], 1)
    totals["uncovered_pct_by_length"] = round(100.0 - totals["coverage_pct_by_length"], 1)

    return {"coverage_by_highway": by_type, "totals": totals}


def build_streets_geojson(
    covered_edges: gpd.GeoDataFrame,
    *,
    city_id: str,
    provider: str,
    run_date: str,
    match_dist_m: float,
    source_csv: str,
) -> dict[str, Any]:
    """
    Assemble the published GeoJSON FeatureCollection.

    Each feature is a street segment with coverage properties; the collection's
    top-level ``properties.metadata`` carries the by-type/overall summary so the
    frontend can draw both the map layer and the breakdown chart from one file.
    """
    summary = summarize_coverage(covered_edges)

    features: list[dict[str, Any]] = []
    for row in covered_edges.itertuples(index=False):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom.__geo_interface__,
                "properties": {
                    "highway": row.highway_bucket,
                    "length_m": round(float(row.length_m), 1),
                    "covered": bool(row.covered),
                    "nearest_pano_date": row.nearest_pano_date,
                    "nearest_pano_age_years": row.nearest_pano_age_years,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "properties": {
            "metadata": {
                "schema_version": 1,
                "kind": "street_coverage",
                "city_id": city_id,
                "provider": provider,
                "run_date": run_date,
                "match_dist_m": match_dist_m,
                "source_csv": source_csv,
                **summary,
            }
        },
        "features": features,
    }


# ── Road-walk fractional coverage (issue #99) ──────────────────────────────
#
# Unlike the grid-attribution path above (a boolean per edge from nearest-pano
# snapping), the road-walk collector queried GSV at on-street points sampled
# along each edge, so association is by construction and coverage is FRACTIONAL:
# an edge's coverage is the fraction of its sample points that returned a
# qualifying nearby pano.


def compute_streetwalk_coverage(
    edges: gpd.GeoDataFrame,
    samples: pd.DataFrame,
    collected: pd.DataFrame,
    run_date: str,
    provider: str = "gsv",
    match_dist_m: float = DEFAULT_MATCH_DIST_M,
) -> gpd.GeoDataFrame:
    """
    Score fractional per-edge coverage from road-walk collection results.

    Each on-street sample point is "covered" when its collected metadata row is
    ``status == 'OK'``, official (for GSV, exact ``© Google``; other providers
    accept any OK pano), and the returned pano lies within ``match_dist_m`` of
    the sample point — the distance guard rejects a pano that snapped to a
    parallel/neighbouring road. An edge's ``coverage_fraction`` is its covered
    samples over its total samples.

    Args:
        edges: WGS84 LineString GeoDataFrame with ``edge_id`` (+ optional
            ``highway``/``length``), from ``graph_to_edges``.
        samples: the deterministic sample list from
            ``road_sampling.generate_samples`` (``edge_id, sample_idx, lat, lon``).
        collected: the raw METADATA_DTYPES DataFrame from the collector's
            csv.gz (``query_lat/query_lon/status/copyright_info/capture_date/
            pano_lat/pano_lon``).
        run_date: ``YYYY-MM-DD``; ages are pinned to it (deterministic).
        provider: imagery provider (governs the official-pano filter).
        match_dist_m: max sample-to-pano distance in metres.

    Returns:
        A copy of ``edges`` (WGS84, RangeIndex) with added columns:
        ``highway_bucket``, ``length_m``, ``total_samples``, ``covered_samples``,
        ``coverage_fraction`` (0..1), ``covered`` (bool: any coverage),
        ``nearest_pano_date`` (str|None, newest covered), and
        ``median_covered_age_years`` (float|None).
    """
    run_ts = pd.Timestamp(run_date)
    out = edges.reset_index(drop=True).copy()
    if "highway" in out.columns:
        out["highway_bucket"] = _bucket_series(out)
    else:
        out["highway_bucket"] = "other"

    # Empty network → well-formed 0-edge frame (see compute_street_coverage).
    if out.empty:
        for col, dtype in (
            ("length_m", float),
            ("total_samples", int),
            ("covered_samples", int),
            ("coverage_fraction", float),
            ("covered", bool),
            ("covered_samples_any", int),
            ("coverage_fraction_any", float),
            ("nearest_pano_date", object),
            ("median_covered_age_years", object),
        ):
            out[col] = pd.Series([], dtype=dtype)
        return out

    metric_crs = out.estimate_utm_crs()
    edges_m = out.to_crs(metric_crs)
    if "length" in out.columns:
        out["length_m"] = pd.to_numeric(out["length"], errors="coerce").fillna(edges_m.length)
    else:
        out["length_m"] = edges_m.length

    # Match each sample to its single collected row via the quantized-coord key
    # (a csv.gz round-trip perturbs floats below the 9-decimal round).
    coll = collected.copy()
    coll["_key"] = [
        quantize_coord(la, lo) for la, lo in zip(coll["query_lat"], coll["query_lon"], strict=True)
    ]
    coll = coll.drop_duplicates("_key", keep="first")
    samp = samples.copy()
    samp["_key"] = [quantize_coord(la, lo) for la, lo in zip(samp["lat"], samp["lon"], strict=True)]
    m = samp.merge(
        coll[["_key", "status", "copyright_info", "capture_date", "pano_lat", "pano_lon"]],
        on="_key",
        how="left",
    )

    # Per-sample covered test: OK + official + a pano within the threshold.
    ok = m["status"] == "OK"
    if provider == "gsv":
        official = (
            is_google_copyright(m["copyright_info"])
            if "copyright_info" in m.columns
            else pd.Series(False, index=m.index)
        )
    else:
        official = pd.Series(True, index=m.index)
    has_pano = m["pano_lat"].notna() & m["pano_lon"].notna()

    sample_pts = gpd.GeoSeries(gpd.points_from_xy(m["lon"], m["lat"]), crs=WGS84).to_crs(metric_crs)
    pano_pts = gpd.GeoSeries(
        gpd.points_from_xy(
            pd.to_numeric(m["pano_lon"], errors="coerce"),
            pd.to_numeric(m["pano_lat"], errors="coerce"),
        ),
        crs=WGS84,
    ).to_crs(metric_crs)
    # distance() yields NaN where the pano point is missing; NaN <= x is False.
    within = sample_pts.distance(pano_pts) <= match_dist_m
    m["covered_sample"] = ok & official & has_pano & within.to_numpy()

    # Any-imagery coverage (issue #116's vocabulary, applied to streets): a
    # sample whose only nearby imagery is flat/perspective carries a FLAT_ONLY
    # row — imagery of the street, but not a 360° pano, and with no capture
    # date. It counts toward `*_any` only, never toward the 360° numbers or any
    # dated statistic. GSV emits no FLAT_ONLY rows, so for GSV the any-value is
    # identical to the 360° one by construction.
    flat_only = (m["status"] == FLAT_ONLY) & has_pano & within.to_numpy()
    m["covered_sample_any"] = m["covered_sample"] | flat_only

    cap = pd.to_datetime(m["capture_date"], errors="coerce")
    m["age_years"] = (run_ts - cap).dt.total_seconds() / _YEAR_SECONDS

    # Aggregate to per-edge fractions. Edges with no samples (empty/zero-length
    # geometry) never appear in `samples`; they default to 0 coverage below.
    grp = m.groupby("edge_id")
    total = grp.size()
    covered = grp["covered_sample"].sum()
    covered_any = grp["covered_sample_any"].sum()

    def _edge_stats(sub: pd.DataFrame) -> pd.Series:
        cov = sub[sub["covered_sample"]]
        # capture_date may arrive as ISO strings (raw CSV) or datetimes (parsed);
        # normalize the newest to a 'YYYY-MM-DD' string for the JSON artifact.
        dates = pd.to_datetime(cov["capture_date"], errors="coerce").dropna()
        ages = cov["age_years"].dropna()
        return pd.Series(
            {
                "nearest_pano_date": dates.max().date().isoformat() if len(dates) else None,
                "median_covered_age_years": round(float(ages.median()), 3) if len(ages) else None,
            }
        )

    extra = grp[["covered_sample", "capture_date", "age_years"]].apply(_edge_stats)

    out["total_samples"] = out["edge_id"].map(total).fillna(0).astype(int)
    out["covered_samples"] = out["edge_id"].map(covered).fillna(0).astype(int)
    out["coverage_fraction"] = (
        (out["covered_samples"] / out["total_samples"].where(out["total_samples"] > 0))
        .fillna(0.0)
        .round(4)
    )
    out["covered"] = out["covered_samples"] > 0
    out["covered_samples_any"] = out["edge_id"].map(covered_any).fillna(0).astype(int)
    out["coverage_fraction_any"] = (
        (out["covered_samples_any"] / out["total_samples"].where(out["total_samples"] > 0))
        .fillna(0.0)
        .round(4)
    )
    # Edges with no covered samples carry NaN here; build_streetwalk_geojson
    # converts NaN -> None at serialization so the JSON artifact stays valid.
    out["nearest_pano_date"] = out["edge_id"].map(
        extra["nearest_pano_date"] if "nearest_pano_date" in extra else pd.Series(dtype=object)
    )
    out["median_covered_age_years"] = out["edge_id"].map(
        extra["median_covered_age_years"]
        if "median_covered_age_years" in extra
        else pd.Series(dtype=object)
    )
    return out


def summarize_streetwalk_coverage(covered_edges: gpd.GeoDataFrame) -> dict[str, Any]:
    """
    Aggregate fractional per-edge coverage into by-highway-type and overall
    stats. Reports coverage by street length (fraction-weighted), plus the
    edge-level mean fraction and fully-covered count.
    """

    # Pre-#116 frames (and any hand-built test fixture) may lack the any-imagery
    # column; treating it as the 360° fraction reproduces the GSV identity and
    # keeps old callers working.
    if "coverage_fraction_any" not in covered_edges.columns:
        covered_edges = covered_edges.assign(
            coverage_fraction_any=covered_edges["coverage_fraction"]
        )

    def _block(group: pd.DataFrame) -> dict[str, Any]:
        edges = int(len(group))
        length_km = float(group["length_m"].sum()) / 1000.0
        # Length credited proportionally to each edge's covered fraction.
        length_km_covered = float((group["length_m"] * group["coverage_fraction"]).sum()) / 1000.0
        length_km_covered_any = (
            float((group["length_m"] * group["coverage_fraction_any"]).sum()) / 1000.0
        )
        sampled = group[group["total_samples"] > 0]
        ages = pd.to_numeric(group["median_covered_age_years"], errors="coerce").dropna()
        return {
            "edges": edges,
            "edges_sampled": int(len(sampled)),
            "edges_fully_covered": int((group["coverage_fraction"] >= 1.0).sum()),
            "edges_any_coverage": int((group["coverage_fraction"] > 0).sum()),
            "length_km": round(length_km, 3),
            "length_km_covered": round(length_km_covered, 3),
            "length_km_covered_any": round(length_km_covered_any, 3),
            "mean_edge_coverage": round(float(sampled["coverage_fraction"].mean()), 4)
            if len(sampled)
            else 0.0,
            "coverage_pct_by_length": round(100.0 * length_km_covered / length_km, 1)
            if length_km
            else 0.0,
            # Imagery of any type (360° + flat). Equals coverage_pct_by_length
            # for GSV, which never emits FLAT_ONLY.
            "coverage_pct_by_length_any": round(100.0 * length_km_covered_any / length_km, 1)
            if length_km
            else 0.0,
            "median_covered_age_years": round(float(ages.median()), 2) if len(ages) else None,
        }

    by_type = {bucket: _block(group) for bucket, group in covered_edges.groupby("highway_bucket")}
    by_type = dict(sorted(by_type.items(), key=lambda kv: _bucket_order(kv[0])))

    totals = _block(covered_edges)
    totals["uncovered_pct_by_length"] = round(100.0 - totals["coverage_pct_by_length"], 1)

    return {"coverage_by_highway": by_type, "totals": totals}


def build_streetwalk_geojson(
    covered_edges: gpd.GeoDataFrame,
    *,
    city_id: str,
    provider: str,
    run_date: str,
    spacing_m: float,
    match_dist_m: float,
    source_csv: str,
    network_type: str = "drive",
) -> dict[str, Any]:
    """
    Assemble the published road-walk coverage GeoJSON FeatureCollection.

    Each feature is a street edge carrying its fractional coverage and sample
    counts; ``properties.metadata`` holds the by-type/overall summary so the
    frontend can draw both the map layer and the breakdown from one file.

    ``network_type`` is recorded in the metadata because it sets which edge
    classes could appear at all: a 'drive' artifact has no footway/path/alley
    features by construction, so a reader must not mistake their absence for a
    city with no footpaths. Defaults to 'drive' for callers predating the
    broader networks.
    """
    summary = summarize_streetwalk_coverage(covered_edges)

    def _none_if_nan(value: Any) -> Any:
        return None if value is None or pd.isna(value) else value

    features: list[dict[str, Any]] = []
    for row in covered_edges.itertuples(index=False):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        median_age = _none_if_nan(row.median_covered_age_years)
        features.append(
            {
                "type": "Feature",
                "geometry": geom.__geo_interface__,
                "properties": {
                    "edge_id": row.edge_id,
                    "highway": row.highway_bucket,
                    "length_m": round(float(row.length_m), 1),
                    "total_samples": int(row.total_samples),
                    "covered_samples": int(row.covered_samples),
                    "coverage_fraction": float(row.coverage_fraction),
                    "covered": bool(row.covered),
                    "covered_samples_any": int(
                        getattr(row, "covered_samples_any", row.covered_samples)
                    ),
                    "coverage_fraction_any": float(
                        getattr(row, "coverage_fraction_any", row.coverage_fraction)
                    ),
                    "nearest_pano_date": _none_if_nan(row.nearest_pano_date),
                    "median_covered_age_years": float(median_age)
                    if median_age is not None
                    else None,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "properties": {
            "metadata": {
                "schema_version": 1,
                "kind": "streetwalk_coverage",
                "city_id": city_id,
                "provider": provider,
                "network_type": network_type,
                "run_date": run_date,
                "spacing_m": spacing_m,
                "match_dist_m": match_dist_m,
                "source_csv": source_csv,
                **summary,
            }
        },
        "features": features,
    }
