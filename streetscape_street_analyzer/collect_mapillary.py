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
import numpy as np
import pandas as pd

from streetscape_metadata_tracker.analysis import FLAT_ONLY
from streetscape_metadata_tracker.download_mapillary import (
    build_empty_rows,
    build_image_rows,
    captured_at_to_iso_dates,
    fetch_city_images_async,
    grid_bbox,
    status_for_capture_dates,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"

# Sample points per sjoin_nearest call. The join's peak memory is driven by the
# match set it materializes, so a big city (hundreds of thousands of samples
# against a multi-million-image census) is chunked rather than joined in one go.
# Purely a memory knob — the result is identical at any block size, since each
# sample's nearest image is decided independently.
_JOIN_CHUNK_SIZE = 50_000


def nearest_images_to_samples(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each sample location, the nearest 360° pano and the nearest flat image
    within ``match_dist_m``.

    Uses ``gpd.sjoin_nearest`` on the local UTM CRS — the same idiom the
    grid-attribution path uses to match panos to edges (street_coverage.py), so
    distances are true metres rather than degree approximations. Panos and
    flats are joined separately because they answer different questions: 360°
    coverage and any-imagery coverage (issue #116's distinction, applied to
    streets).

    The samples run through the join in ``_JOIN_CHUNK_SIZE`` blocks rather than
    one call. Now that ``mapillary_streets`` is scheduled across the whole
    catalog, a dense city pairs a multi-million-image census with a few hundred
    thousand sample points, and a single join materializes the whole match set
    at once. Chunking bounds peak memory at the census plus one block's matches;
    it cannot change the result, because a sample never spans two blocks and the
    nearest-image choice is per sample.

    Args:
        query_points: ``(lat, lon, seq, _)`` tuples from
            ``road_sampling.dedupe_query_points``.
        census: the columnar Mapillary census (``fetch_city_images_async``).
        match_dist_m: max sample-to-image distance in metres.

    Returns:
        ``(pano_positions, flat_positions)`` — two int arrays as long as
        ``query_points``, holding the census position of the nearest qualifying
        image per sample and ``-1`` where nothing is in range. Positions rather
        than image records: Colorado Springs pairs 360k samples with a 6.5M
        image census, and a dict of per-sample image dicts is a copy of the
        census the arrays do not need (issue #157).
    """
    n_samples = len(query_points)
    none_matched = np.full(n_samples, -1, dtype=np.int64)
    if not n_samples or not len(census):
        return none_matched, none_matched.copy()

    samples_gdf = gpd.GeoDataFrame(
        {"sample_idx": range(n_samples)},
        geometry=gpd.points_from_xy([p[1] for p in query_points], [p[0] for p in query_points]),
        crs=WGS84,
    )
    # A city's sample points span a single UTM zone in every realistic case;
    # estimate_utm_crs picks it from the sample extent.
    metric_crs = samples_gdf.estimate_utm_crs()
    samples_m = samples_gdf.to_crs(metric_crs)

    def _join(positions: np.ndarray) -> np.ndarray:
        matches = np.full(n_samples, -1, dtype=np.int64)
        if not len(positions):
            return matches
        # image_idx carries the position in the FULL census, not in this
        # subset, so the caller can index the census directly.
        # Built once, outside the chunk loop: the census is the same for every
        # block, and reprojecting it per block would dominate the runtime.
        images_gdf = gpd.GeoDataFrame(
            {"image_idx": positions},
            geometry=gpd.points_from_xy(
                census["lon"].to_numpy()[positions], census["lat"].to_numpy()[positions]
            ),
            crs=WGS84,
        ).to_crs(metric_crs)

        for start in range(0, len(samples_m), _JOIN_CHUNK_SIZE):
            block = samples_m.iloc[start : start + _JOIN_CHUNK_SIZE]
            joined = gpd.sjoin_nearest(
                block,
                images_gdf,
                how="inner",
                max_distance=match_dist_m,
                distance_col="dist_m",
            )
            # sjoin_nearest emits every tied nearest neighbour; keep the closest
            # single image per sample so each sample yields exactly one row.
            joined = joined.sort_values("dist_m").drop_duplicates("sample_idx", keep="first")
            matches[joined["sample_idx"].to_numpy()] = joined["image_idx"].to_numpy()
        return matches

    is_pano = census["is_pano"].to_numpy()
    return _join(np.flatnonzero(is_pano)), _join(np.flatnonzero(~is_pano))


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
    query_timestamp: str,
) -> pd.DataFrame:
    """
    Score every sample location against the census into METADATA rows.

    Exactly one row per sample, in ``query_points`` order. Status vocabulary
    matches the grid downloader exactly (issue #116):

      * ``OK`` / ``NO_DATE`` — a 360° pano is in range (NO_DATE when its
        contributor timestamp is unusable), which is what 360° street coverage
        counts.
      * ``FLAT_ONLY`` — no pano in range but flat/perspective imagery is. A
        presence marker with a **null capture_date**, so flat timestamps never
        enter a dated statistic; counts only toward any-imagery coverage.
      * ``ZERO_RESULTS`` — no imagery of any kind in range.
    """
    pano_positions, flat_positions = nearest_images_to_samples(query_points, census, match_dist_m)
    sample_lats = np.array([p[0] for p in query_points], dtype=np.float64)
    sample_lons = np.array([p[1] for p in query_points], dtype=np.float64)

    has_pano = pano_positions >= 0
    # A pano wins wherever there is one; a flat only speaks for samples no pano
    # reached, which is exactly what makes the row FLAT_ONLY rather than OK.
    matched = has_pano | (flat_positions >= 0)
    chosen = np.where(has_pano, pano_positions, flat_positions)

    matched_idx = np.flatnonzero(matched)
    capture_dates = np.full(len(matched_idx), None, dtype=object)
    pano_of_matched = has_pano[matched_idx]
    capture_dates[pano_of_matched] = captured_at_to_iso_dates(
        census["captured_at_ms"].to_numpy()[chosen[matched_idx][pano_of_matched]]
    ).to_numpy()
    # status_for_capture_dates is evaluated over every matched row, including
    # the flat ones whose capture_date is None — those transiently read "OK"
    # (None != "") and are then overridden by the outer where. Kept whole-array
    # rather than masked so the OK/NO_DATE rule has exactly one statement,
    # shared with the grid downloader; a flat row's status never escapes it.
    status = np.where(~pano_of_matched, FLAT_ONLY, status_for_capture_dates(capture_dates))
    image_rows = build_image_rows(
        census,
        chosen[matched_idx],
        sample_lats[matched_idx],
        sample_lons[matched_idx],
        query_timestamp,
        status,
        capture_dates,
    )

    empty_idx = np.flatnonzero(~matched)
    if not len(empty_idx):
        return image_rows
    empty_rows = build_empty_rows(
        sample_lats[empty_idx],
        sample_lons[empty_idx],
        query_timestamp,
        "ZERO_RESULTS",
    )
    # Restore sample order: the two frames were built by kind, not by position.
    combined = pd.concat([image_rows, empty_rows], ignore_index=True)
    return combined.iloc[
        np.argsort(np.concatenate([matched_idx, empty_idx]), kind="stable")
    ].reset_index(drop=True)


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
    # pop, not [] — `fetched` is a live local until this function returns, so
    # indexing it would keep the whole census resident past the `del` below,
    # through the join, the row build and the CSV write.
    census = fetched.pop("census")
    num_flat_images = int((~census["is_pano"].to_numpy()).sum())
    logger.info(
        "%s: %d Mapillary images (%d panos, %d flat) from %d tiles → scoring %d sample points",
        city.city_id,
        len(census),
        len(census) - num_flat_images,
        num_flat_images,
        fetched["tiles"],
        len(query_points),
    )

    df = build_streetwalk_rows(query_points, census, match_dist_m, started_at)
    del census
    # Straight into the gzip handle: to_csv() with no path builds the whole CSV
    # as a str and then a second copy as bytes, which at a big city's sample
    # count is pure duplication of a file being written out anyway (issue #157).
    with gzip.open(output_csv_gz_path, "wt", encoding="utf-8", newline="") as f:
        df.to_csv(f, index=False)

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
