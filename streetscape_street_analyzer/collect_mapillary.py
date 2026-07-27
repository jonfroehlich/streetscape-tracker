"""
Mapillary road-walk sample collection (issue #99, Mapillary arm).

The GSV road walk issues one metadata request per on-street sample point — a
quarter of a million requests for a city like Seattle. Mapillary has no
per-point metadata endpoint at all: it publishes a **z14 vector-tile census**,
a few dozen tile requests for an entire city, each tile carrying every image's
position and metadata. So the Mapillary "walk" is not a walk of requests; it is
a purely local spatial join of that census onto the same on-street sample
points the GSV walk uses.

Two consequences worth keeping in mind when reading the numbers:

  * **Cost.** A Mapillary street collection costs what a Mapillary grid run
    costs (tens of tile requests), independent of city size or sample spacing.
  * **Comparability.** Both providers score the *same* deterministic sample
    points from the same frozen OSM network, so their `coverage_pct_by_length`
    are directly comparable — unlike raw pano counts, which are census vs.
    sample (see the provider model in CLAUDE.md).

The output is a METADATA-schema snapshot, one row per unique sample location,
exactly like the GSV collector's — so everything downstream
(`compute_streetwalk_coverage`, the coverage GeoJSON, the catalog row, the
manifest, the frontend) is shared, not duplicated.
"""

from __future__ import annotations

import gzip
import logging
import os
from datetime import UTC, datetime
from typing import Any

import geopandas as gpd
import pandas as pd

from streetscape_metadata_tracker.analysis import FLAT_ONLY
from streetscape_metadata_tracker.config import MAPILLARY_METADATA_DTYPES
from streetscape_metadata_tracker.download_mapillary import (
    captured_at_to_iso_date,
    fetch_city_images_async,
    grid_bbox,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"


def _image_row(
    img: dict[str, Any],
    query_lat: float,
    query_lon: float,
    query_timestamp: str,
    status: str,
    capture_date: str | None,
) -> dict[str, Any]:
    """One METADATA row for a sample location matched to a Mapillary image.

    Mirrors download_mapillary._image_row (same columns, same copyright
    convention) but keyed to an on-street sample location rather than a grid
    point.
    """
    creator = img["creator_id"]
    return {
        "query_lat": query_lat,
        "query_lon": query_lon,
        "query_timestamp": query_timestamp,
        "pano_lat": img["lat"],
        "pano_lon": img["lon"],
        "pano_id": img["id"],
        "capture_date": capture_date,
        "copyright_info": (
            f"© Mapillary contributor {creator}" if creator is not None else "© Mapillary"
        ),
        "status": status,
        "creator_id": (None if creator is None else str(creator)),
        "organization_id": img["organization_id"],
        "sequence_id": img["sequence_id"],
        "is_pano": img["is_pano"],
        "on_foot": img["on_foot"],
        "quality_score": img["quality_score"],
        "compass_angle": img["compass_angle"],
    }


def _empty_row(query_lat: float, query_lon: float, query_timestamp: str) -> dict[str, Any]:
    """A sample location with no Mapillary imagery within the match distance."""
    return {
        "query_lat": query_lat,
        "query_lon": query_lon,
        "query_timestamp": query_timestamp,
        "pano_lat": None,
        "pano_lon": None,
        "pano_id": None,
        "capture_date": None,
        "copyright_info": None,
        "status": "ZERO_RESULTS",
        "creator_id": None,
        "organization_id": None,
        "sequence_id": None,
        "is_pano": None,
        "on_foot": None,
        "quality_score": None,
        "compass_angle": None,
    }


def nearest_images_to_samples(
    query_points: list[tuple[float, float, int, int]],
    images: list[dict[str, Any]],
    match_dist_m: float,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    For each sample location, the nearest 360° pano and the nearest flat image
    within ``match_dist_m``.

    Uses ``gpd.sjoin_nearest`` on the local UTM CRS — the same idiom the
    grid-attribution path uses to match panos to edges (street_coverage.py), so
    distances are true metres rather than degree approximations. Panos and
    flats are joined separately because they answer different questions: 360°
    coverage and any-imagery coverage (issue #116's distinction, applied to
    streets).

    Args:
        query_points: ``(lat, lon, seq, _)`` tuples from
            ``road_sampling.dedupe_query_points``.
        images: decoded Mapillary image dicts (``fetch_city_images_async``).
        match_dist_m: max sample-to-image distance in metres.

    Returns:
        ``(pano_by_index, flat_by_index)`` — dicts keyed by the sample's
        position in ``query_points``, holding the nearest qualifying image.
        A sample with nothing in range is absent from both.
    """
    if not query_points:
        return {}, {}

    samples_gdf = gpd.GeoDataFrame(
        {"sample_idx": range(len(query_points))},
        geometry=gpd.points_from_xy(
            [p[1] for p in query_points], [p[0] for p in query_points]
        ),
        crs=WGS84,
    )
    # A city's sample points span a single UTM zone in every realistic case;
    # estimate_utm_crs picks it from the sample extent.
    metric_crs = samples_gdf.estimate_utm_crs()
    samples_m = samples_gdf.to_crs(metric_crs)

    def _join(subset: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        if not subset:
            return {}
        images_gdf = gpd.GeoDataFrame(
            {"image_idx": range(len(subset))},
            geometry=gpd.points_from_xy(
                [img["lon"] for img in subset], [img["lat"] for img in subset]
            ),
            crs=WGS84,
        ).to_crs(metric_crs)
        joined = gpd.sjoin_nearest(
            samples_m,
            images_gdf,
            how="inner",
            max_distance=match_dist_m,
            distance_col="dist_m",
        )
        # sjoin_nearest emits every tied nearest neighbour; keep the closest
        # single image per sample so each sample yields exactly one row.
        joined = joined.sort_values("dist_m").drop_duplicates("sample_idx", keep="first")
        return {
            int(row.sample_idx): subset[int(row.image_idx)]
            for row in joined.itertuples(index=False)
        }

    panos = [img for img in images if img["is_pano"]]
    flats = [img for img in images if not img["is_pano"]]
    return _join(panos), _join(flats)


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    images: list[dict[str, Any]],
    match_dist_m: float,
    query_timestamp: str,
) -> list[dict[str, Any]]:
    """
    Score every sample location against the census into METADATA rows.

    Status vocabulary matches the grid downloader exactly (issue #116):

      * ``OK`` / ``NO_DATE`` — a 360° pano is in range (NO_DATE when its
        contributor timestamp is unusable), which is what 360° street coverage
        counts.
      * ``FLAT_ONLY`` — no pano in range but flat/perspective imagery is. A
        presence marker with a **null capture_date**, so flat timestamps never
        enter a dated statistic; counts only toward any-imagery coverage.
      * ``ZERO_RESULTS`` — no imagery of any kind in range.
    """
    pano_by_index, flat_by_index = nearest_images_to_samples(
        query_points, images, match_dist_m
    )

    rows = []
    for idx, (lat, lon, _seq, _unused) in enumerate(query_points):
        pano = pano_by_index.get(idx)
        if pano is not None:
            capture_date = captured_at_to_iso_date(pano["captured_at_ms"])
            rows.append(
                _image_row(
                    pano,
                    lat,
                    lon,
                    query_timestamp,
                    "OK" if capture_date else "NO_DATE",
                    capture_date,
                )
            )
            continue
        flat = flat_by_index.get(idx)
        if flat is not None:
            rows.append(_image_row(flat, lat, lon, query_timestamp, FLAT_ONLY, None))
            continue
        rows.append(_empty_row(lat, lon, query_timestamp))
    return rows


async def collect_mapillary_street_samples_async(
    query_points: list[tuple[float, float, int, int]],
    city,
    access_token: str,
    output_csv_gz_path: str,
    match_dist_m: float,
    connection_limit: int = 5,
    request_timeout: float = 30,
) -> dict[str, Any]:
    """
    Collect Mapillary street samples for a city and write the snapshot csv.gz.

    Returns the same contract as ``download_gsv.collect_points_async`` (``df``,
    ``filename_with_path``, ``api_requests``, ``started_at``, ``finished_at``)
    plus ``num_flat_images``, so ``collect.py`` treats both providers
    identically after the download step.

    The census is fetched over the city's **frozen grid bbox** — the same
    footprint the Mapillary grid run uses — so a street walk can never reach
    imagery outside the area the city is defined to cover.
    """
    started_at = datetime.now(UTC).isoformat()
    if not output_csv_gz_path.endswith(".csv.gz"):
        raise ValueError(f"output_csv_gz_path must end in .csv.gz, got: {output_csv_gz_path}")
    os.makedirs(os.path.dirname(os.path.abspath(output_csv_gz_path)), exist_ok=True)

    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    fetched = await fetch_city_images_async(
        city.display_name,
        bbox,
        access_token,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
    )
    images = fetched["images"]
    num_flat_images = sum(1 for img in images if not img["is_pano"])
    logger.info(
        "%s: %d Mapillary images (%d panos, %d flat) from %d tiles → scoring %d sample points",
        city.city_id,
        len(images),
        len(images) - num_flat_images,
        num_flat_images,
        fetched["tiles"],
        len(query_points),
    )

    rows = build_streetwalk_rows(query_points, images, match_dist_m, started_at)
    df = pd.DataFrame(rows, columns=list(MAPILLARY_METADATA_DTYPES.keys()))
    with gzip.open(output_csv_gz_path, "wb") as f:
        f.write(df.to_csv(index=False).encode("utf-8"))

    # Read back through the shared loader so dtypes match the GSV path exactly.
    df = load_city_csv_file(output_csv_gz_path)
    return {
        "df": df,
        "filename_with_path": output_csv_gz_path,
        "api_requests": fetched["api_requests"],
        "num_flat_images": num_flat_images,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
