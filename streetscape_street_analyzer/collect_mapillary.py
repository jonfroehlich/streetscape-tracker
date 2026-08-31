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
    costs — the z14 tile count over the frozen bbox — which is independent of
    sample spacing (that half is pinned by a test) but NOT of city size: tile
    count scales with bbox area, measured at a median of 12 and a max of 870
    across the catalog on 2026-08-16. Compare a GSV walk, which is one request
    per sample point and so runs into the hundreds of thousands.
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

import numpy as np
import pandas as pd

from streetscape_metadata_tracker.census import census_is_pano
from streetscape_metadata_tracker.checkpointing import CensusCache, observation_timestamp
from streetscape_metadata_tracker.download_mapillary import (
    DEFAULT_TILE_JITTER,
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    build_empty_rows,
    build_image_rows,
    captured_at_to_iso_dates,
    fetch_city_images_async,
    grid_bbox,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_street_analyzer.census_walk import CensusWalkSpec
from streetscape_street_analyzer.census_walk import (
    build_streetwalk_rows as census_walk_rows,
)

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"


# Mapillary's binding of the shared census→walk scorer (issue #258).
#
# The join, the pano-beats-flat rule and the OK/NO_DATE/FLAT_ONLY/ZERO_RESULTS
# vocabulary all live in census_walk now; what is left here is the one piece
# that genuinely differs -- Mapillary stores capture time as epoch milliseconds
# -- plus this module's own output-schema bindings.
def _mapillary_capture_dates(census: pd.DataFrame, positions: np.ndarray):
    """ISO capture dates for those census rows, from Mapillary's epoch ms."""
    return captured_at_to_iso_dates(census["captured_at_ms"].to_numpy()[positions]).to_numpy()


MAPILLARY_WALK = CensusWalkSpec(
    capture_dates_for=_mapillary_capture_dates,
    build_image_rows=build_image_rows,
    build_empty_rows=build_empty_rows,
)


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
    query_timestamp: str,
) -> pd.DataFrame:
    """
    Score Mapillary census images against the walk's sample points.

    A thin binding of :func:`census_walk.build_streetwalk_rows`; the contract
    and the status vocabulary are documented there. Kept as a named function on
    this module rather than a bare partial so the collector below still resolves
    it as a module global -- which is what lets a test substitute it to simulate
    a tail failure after the census is already paid for.
    """
    return census_walk_rows(query_points, census, match_dist_m, query_timestamp, MAPILLARY_WALK)


async def collect_mapillary_street_samples_async(
    query_points: list[tuple[float, float, int, int]],
    city,
    access_token: str,
    output_csv_gz_path: str,
    match_dist_m: float,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Collect Mapillary street samples for a city and write the snapshot csv.gz.

    Returns the same contract as ``download_gsv.collect_points_async`` (``df``,
    ``filename_with_path``, ``api_requests``, ``started_at``, ``finished_at``)
    plus ``num_flat_images``, so ``collect.py`` treats both providers
    identically after the download step — and, when checkpointing is on,
    ``api_requests_total`` (the census's spend across resumes, for the
    ``street_walks`` row) and ``checkpoint_path`` (the caller's to discard
    once that row is committed).

    The census is fetched over the city's **frozen grid bbox** — the same
    footprint the Mapillary grid run uses — so a street walk can never reach
    imagery outside the area the city is defined to cover.

    THAT IDENTITY IS WHAT ``cache_path`` EXPLOITS (issue #290). The grid run and
    this walk read the same tiles over the same bbox, so on a paired night the
    grid run pays and this walk reads its census from the shared cache for zero
    requests — and a second walk at another ``--network-type`` is free as well.
    ``census_cache.reuse=False`` (``--refetch-census``) opts out. When the census IS
    reused, every row's ``query_timestamp`` records when the provider was
    observed rather than when this process started, and the return carries
    ``census_fetched_by``/``census_fetched_at`` for the ``street_walks`` row.
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
        max_requests_per_minute=max_requests_per_minute,
        jitter=jitter,
        checkpoint_path=checkpoint_path,
        checkpoint_channel=checkpoint_channel,
        checkpoint_variant=checkpoint_variant,
        census_cache=census_cache,
    )
    # A reused census is stamped with when the provider was observed, a fresh
    # one with this process's clock; see checkpointing.observation_timestamp.
    query_timestamp = observation_timestamp(fetched, started_at)
    # THE TAIL IS WRAPPED BECAUSE THE CHECKPOINT CHANGES WHAT A CRASH HERE COSTS
    # (#256). Without one, a failure below lost the spend with the process and
    # the caller recorded whatever the exception carried. With one, the
    # checkpoint survives complete and the NEXT invocation re-finalizes it for
    # ZERO requests — so a tail failure that carried no spend would land this
    # census's tiles in no api_usage row, EVER. That is the gap PR #251 closed
    # for KartaView and download_mapillary_metadata_async closes for the grid
    # run; the walk needs it for the same reason and did not have it.
    try:
        # pop, not [] — `fetched` is a live local until this function returns, so
        # indexing it would keep the whole census resident past the `del` below,
        # through the join, the row build and the CSV write.
        census = fetched.pop("census")
        num_flat_images = int((~census_is_pano(census)).sum())
        logger.info(
            "%s: %d Mapillary images (%d panos, %d flat) from %d tiles → scoring %d sample points",
            city.city_id,
            len(census),
            len(census) - num_flat_images,
            num_flat_images,
            fetched["tiles"],
            len(query_points),
        )

        df = build_streetwalk_rows(query_points, census, match_dist_m, query_timestamp)
        del census
        # Straight into the gzip handle: to_csv() with no path builds the whole CSV
        # as a str and then a second copy as bytes, which at a big city's sample
        # count is pure duplication of a file being written out anyway (issue #157).
        with gzip.open(output_csv_gz_path, "wt", encoding="utf-8", newline="") as f:
            df.to_csv(f, index=False)

        # Read back through the shared loader so dtypes match the GSV path exactly.
        df = load_city_csv_file(output_csv_gz_path)
    except BaseException as e:
        e.api_requests = fetched["api_requests"]
        e.api_requests_total = fetched["api_requests_total"]
        raise
    return {
        "df": df,
        "filename_with_path": output_csv_gz_path,
        # This process's spend, for the additive daily ledger; the cumulative
        # figure below is for the street_walks row (#239's rule, #256's census).
        "api_requests": fetched["api_requests"],
        "api_requests_total": fetched["api_requests_total"],
        "checkpoint_path": fetched.get("checkpoint_path"),
        "census_fetched_by": fetched.get("census_fetched_by"),
        "census_fetched_at": fetched.get("census_fetched_at"),
        "census_reused": bool(fetched.get("census_reused")),
        "num_flat_images": num_flat_images,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
