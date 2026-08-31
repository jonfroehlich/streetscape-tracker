"""
KartaView road-walk sample collection (issue #258).

KartaView, like Mapillary, publishes no per-sample-point endpoint. Its census is
a paginated **radius sweep** of overlapping circles over the frozen grid bbox,
so a KartaView "walk" is not a walk of requests at all: it is one sweep plus a
purely local spatial join onto the same deterministic on-street sample points
the GSV walk queries one at a time. The join itself lives in
:mod:`census_walk`; this module supplies the provider half.

Three consequences worth holding onto:

  * **Cost is bbox area, not sample count.** A KartaView street collection costs
    what a KartaView grid run costs -- the swept-circle lattice over the frozen
    bbox -- which is independent of ``--spacing`` (pinned by a test) but not of
    city size: a catalog median of 12 root cells against a max of 4,500.
  * **On a paired night it costs NOTHING.** The census cache (#290) keys on
    (provider, city, bbox) with no channel, variant or date in it, so the grid
    run's sweep minutes earlier is this walk's census for zero requests -- and
    a second ``--network-type`` is free on top of that. This is the whole reason
    the walk is affordable at all; see :func:`checkpointing.crawl_store_for`.
  * **Comparability.** Every provider's walk scores the SAME sample points from
    the same frozen OSM network, so ``coverage_pct_by_length`` is directly
    comparable across providers -- unlike raw pano counts, which are census vs.
    sample (see the provider model in CLAUDE.md).

The output is a METADATA-schema snapshot, one row per unique sample location,
exactly like the GSV and Mapillary collectors' -- so everything downstream
(``compute_streetwalk_coverage``, the coverage GeoJSON, the catalog row, the
manifest, the frontend) is shared, not duplicated. streets.html renders a
KartaView walk the moment one reaches the manifest, with no frontend change.
"""

from __future__ import annotations

import gzip
import logging
import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from streetscape_metadata_tracker.census import census_is_pano
from streetscape_metadata_tracker.checkpointing import CensusCache, observation_timestamp
from streetscape_metadata_tracker.download_kartaview import (
    DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    _kartaview_capture_dates,
    _points_in_cells,
    build_empty_rows,
    build_image_rows,
    fetch_city_images_async,
    grid_bbox,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_street_analyzer.census_walk import CensusWalkSpec
from streetscape_street_analyzer.census_walk import (
    build_streetwalk_rows as census_walk_rows,
)

logger = logging.getLogger(__name__)


# KartaView's binding of the shared census→walk scorer (issue #258).
#
# The date rule is the one genuinely divergent piece, and it is the reason #257
# had to land first: KartaView serves ingest timestamps as capture dates for
# part of its catalog, so `shot_date >= date_added` is rejected -- `>=`, not `>`
# -- which makes NO_DATE a LARGE population here by construction rather than a
# rare edge. An undated pano still covers; it simply ages nothing.
KARTAVIEW_WALK = CensusWalkSpec(
    capture_dates_for=_kartaview_capture_dates,
    build_image_rows=build_image_rows,
    build_empty_rows=build_empty_rows,
)


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
    query_timestamp: str,
    unmeasured_mask=None,
) -> pd.DataFrame:
    """
    Score KartaView census images against the walk's sample points.

    A thin binding of :func:`census_walk.build_streetwalk_rows`; the contract
    and the status vocabulary are documented there. Kept as a named function on
    this module rather than a bare partial so the collector below resolves it as
    a module global, which is what lets a test substitute it to simulate a tail
    failure after the sweep is already paid for.
    """
    return census_walk_rows(
        query_points,
        census,
        match_dist_m,
        query_timestamp,
        KARTAVIEW_WALK,
        unmeasured_mask=unmeasured_mask,
    )


async def collect_kartaview_street_samples_async(
    query_points: list[tuple[float, float, int, int]],
    city,
    access_token: str,
    output_csv_gz_path: str,
    match_dist_m: float,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    max_requests: int | None = None,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Collect KartaView street samples for a city and write the snapshot csv.gz.

    Returns the same contract as ``download_gsv.collect_points_async`` (``df``,
    ``filename_with_path``, ``api_requests``, ``started_at``, ``finished_at``)
    plus ``num_flat_images``, so ``collect.py`` treats every provider
    identically after the download step -- and ``api_requests_total`` (the
    sweep's spend across resumes, for the ``street_walks`` row) and
    ``checkpoint_path`` (the caller's to discard once that row is committed).

    ``api_requests`` is THIS PROCESS's spend and ``api_requests_total`` is the
    sweep's across every resume. They are different numbers and the distinction
    is load-bearing: ``db.add_api_usage`` is additive and keyed by (date,
    provider), so a resumed walk reporting the whole sweep would charge last
    night's requests against tonight's budget gate.

    The census is fetched over the city's **frozen grid bbox** -- the same bbox
    the grid run sweeps -- which is what lets the two share a cache entry and
    makes this walk free on a paired night.
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
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
        max_requests=max_requests,
        checkpoint_path=checkpoint_path,
        checkpoint_channel=checkpoint_channel,
        checkpoint_variant=checkpoint_variant,
        census_cache=census_cache,
    )
    # A reused census is stamped with when the provider was observed, a fresh
    # one with this process's clock; see checkpointing.observation_timestamp.
    query_timestamp = observation_timestamp(fetched, started_at)
    failed_cells = fetched.get("failed_cells") or []
    # THE TAIL IS WRAPPED BECAUSE THE CHECKPOINT CHANGES WHAT A CRASH HERE COSTS
    # (#239/#256). Without one, a failure below lost the spend with the process
    # and the caller recorded whatever the exception carried. With one, the
    # checkpoint survives complete and the NEXT invocation re-finalizes it for
    # ZERO requests — so a tail failure that carried no spend would land this
    # sweep's requests in no api_usage row, EVER. That is the gap PR #251 closed
    # for the KartaView grid run; the walk needs it for the same reason.
    try:
        # pop, not [] — `fetched` is a live local until this function returns, so
        # indexing it would keep the whole census resident past the `del` below,
        # through the join, the row build and the CSV write.
        census = fetched.pop("census")
        num_flat_images = int((~census_is_pano(census)).sum())
        logger.info(
            "%s: %d KartaView images (%d panos, %d flat) from %d cells → scoring %d sample points",
            city.city_id,
            len(census),
            len(census) - num_flat_images,
            num_flat_images,
            fetched.get("cells_visited") or 0,
            len(query_points),
        )

        df = build_streetwalk_rows(
            query_points,
            census,
            match_dist_m,
            query_timestamp,
            # A cell nothing came back for leaves its samples UNKNOWN rather
            # than empty, exactly as the grid run does; a clean sweep passes
            # None and pays nothing. Street coverage is a share of SAMPLES, so
            # publishing an unswept sample as ZERO_RESULTS would read as
            # measured emptiness and understate the city in an immutable dated
            # snapshot. The sweep refuses to finalize past
            # MAX_FAILED_AREA_FRACTION, so this only ever describes a small
            # remainder.
            unmeasured_mask=(
                (lambda lats, lons: _points_in_cells(lats, lons, failed_cells))
                if failed_cells
                else None
            ),
        )
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
        # figure below is for the street_walks row (#239's rule, #290's census).
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


__all__ = [
    "KARTAVIEW_WALK",
    "build_streetwalk_rows",
    "collect_kartaview_street_samples_async",
]
